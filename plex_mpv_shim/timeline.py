import logging
import requests
import threading
import time
import os
from threading import Lock

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

from io import BytesIO
from multiprocessing.dummy import Pool

from .conf import settings
from .media import MediaType
from .player import playerManager
from .subscribers import remoteSubscriberManager
from .utils import Timer, safe_urlopen, mpv_color_to_plex

log = logging.getLogger("timeline")

class TimelineManager(threading.Thread):
    def __init__(self):
        self.currentItems    = {}
        self.currentStates   = {}
        self.idleTimer       = Timer()
        self.subTimer        = Timer()
        self.serverTimer     = Timer()
        self.stopped         = False
        self.halt            = False
        self.trigger         = threading.Event()
        self.is_idle         = True
        self.last_media_item = None
        self.sender_pool     = Pool(5)
        self.sending_to_ps   = Lock()
        self.last_server_url = None

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.sender_pool.close()
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            if (playerManager._player and playerManager._media_item and (not settings.idle_when_paused
                or not playerManager.is_paused())) or force_next:
                if not playerManager.is_paused() or force_next:
                    self.SendTimelineToSubscribers()
                self.delay_idle()
            force_next = False
            if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle:
                if settings.idle_when_paused and settings.stop_idle and playerManager._media_item:
                    playerManager.stop()
                if settings.idle_cmd:
                    os.system(settings.idle_cmd)
                self.is_idle = True
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    def SendTimelineToSubscribers(self):
        timeline = self.GetCurrentTimeline()

        # The sender_pool prevents the timeline from freezing
        # if a client times out or takes a while to respond.

        log.debug("TimelineManager::SendTimelineToSubscribers updating all subscribers")
        for sub in list(remoteSubscriberManager.subscribers.values()):
            self.sender_pool.apply_async(self.SendTimelineToSubscriber, (sub, timeline))
        
        # Also send timeline to plex server.
        # Do not send the timeline if the last one if still sending.
        # (Plex servers can get overloaded... We don't want the UI to freeze.)
        # Note that we send anyway if the state is stopped. We don't want that to get lost.
        if self.sending_to_ps.acquire(False) or timeline["state"] == "stopped":
            self.sender_pool.apply_async(self.SendTimelineToPlexServer, (timeline,))

    def SendTimelineToPlexServer(self, timeline):
        try:
            media_item  = playerManager._media_item
            server_url = None
            if media_item:
                server_url = media_item.parent.server_url
                self.last_server_url = media_item.parent.server_url
            elif self.last_server_url:
                server_url = self.last_server_url
            if server_url:
                safe_urlopen("%s/:/timeline" % server_url, timeline, quiet=True)
        finally:
            self.sending_to_ps.release()

    def SendTimelineToSubscriber(self, subscriber, timeline=None):
        subscriber.set_poll_evt()
        if subscriber.url == "":
            return True

        timelineXML = self.GetCurrentTimeLinesXML(subscriber, timeline)
        url = "%s/:/timeline" % subscriber.url

        log.debug("TimelineManager::SendTimelineToSubscriber sending timeline to %s" % url)

        tree = et.ElementTree(timelineXML)
        tmp  = BytesIO()
        tree.write(tmp, encoding="utf-8", xml_declaration=True)

        tmp.seek(0)
        xmlData = tmp.read()

        # TODO: Abstract this into a utility function and add other X-Plex-XXX fields
        try:
            requests.post(url, data=xmlData, headers={
                "Content-Type":             "application/x-www-form-urlencoded",
                "Connection":               "keep-alive",
                "Content-Range":            "bytes 0-/-1",
                "X-Plex-Client-Identifier": settings.client_uuid
            }, timeout=5)
            return True
        except requests.exceptions.ConnectTimeout:
            log.warning("TimelineManager::SendTimelineToSubscriber timeout sending to %s" % url)
            return False
        except Exception:
            log.warning("TimelineManager::SendTimelineToSubscriber error sending to %s" % url)
            return False

    def WaitForTimeline(self, subscriber):
        subscriber.get_poll_evt().wait(30)
        return self.GetCurrentTimeLinesXML(subscriber)

    def GetCurrentTimeLinesXML(self, subscriber, tlines=None):
        if tlines is None:
            tlines = self.GetCurrentTimeline()

        #
        # Only "video" is supported right now
        #
        mediaContainer = et.Element("MediaContainer")
        if subscriber.commandID is not None:
            mediaContainer.set("commandID", str(subscriber.commandID))
        mediaContainer.set("location", tlines["location"])

        lineEl = et.Element("Timeline")
        for key, value in list(tlines.items()):
            lineEl.set(key, str(value))
        mediaContainer.append(lineEl)

        return mediaContainer

    def GetCurrentTimeline(self):
        # https://github.com/plexinc/plex-home-theater-public/blob/pht-frodo/plex/Client/PlexTimelineManager.cpp#L142
        # Note: location is set to "" to avoid pop-up of navigation menu. This may be abuse of the API.
        options = {
            "location": "",
            "state":    playerManager.get_state(),
            "type":     "video"
        }
        controllable = []

        media_item  = playerManager._media_item
        player = playerManager._player

        # The playback_time value can take on the value of none, probably
        # when playback is complete. This avoids the thread crashing.
        if media_item and not player.playback_abort and player.playback_time:
            self.last_media_item = media_item
            media = media_item.parent

            if media_item.media_type == MediaType.VIDEO:
                options["type"]          = "video"
                options["location"]      = "fullScreenVideo"
            elif media_item.media_type == MediaType.MUSIC:
                options["type"]          = "video"
                options["location"]      = "fullScreenMusic"

            options["time"]              = int(player.playback_time * 1e3)
            options["autoPlay"]          = '1' if settings.auto_play else '0'
            
            aid, sid = playerManager.get_track_ids()

            if aid:
                options["audioStreamID"] = aid
            if sid:
                options["subtitleStreamID"] = sid
                options["subtitleSize"] = settings.subtitle_size
                controllable.append("subtitleSize")
                
                if not media_item.is_transcode:
                    options["subtitlePosition"] = settings.subtitle_position
                    options["subtitleColor"] = mpv_color_to_plex(settings.subtitle_color)
                    controllable.append("subtitlePosition")
                    controllable.append("subtitleColor")

            options["ratingKey"]         = media_item.get_attr("ratingKey")
            options["key"]               = media_item.get_attr("key")
            options["containerKey"]      = media_item.get_attr("key")
            options["guid"]              = media_item.get_attr("guid")
            options["duration"]          = media_item.get_attr("duration", "0")
            options["address"]           = media.path.hostname
            options["protocol"]          = media.path.scheme
            options["port"]              = media.path.port
            options["machineIdentifier"] = media.get_machine_identifier()
            options["seekRange"]         = "0-%s" % options["duration"]

            if media.play_queue:
                options.update(media.get_queue_info())

            controllable.append("playPause")
            controllable.append("stop")
            controllable.append("stepBack")
            controllable.append("stepForward")
            controllable.append("seekTo")
            controllable.append("skipTo")
            controllable.append("autoPlay")

            controllable.append("subtitleStream")
            controllable.append("audioStream")

            if media_item.parent.has_next:
                controllable.append("skipNext")
            
            if media_item.parent.has_prev:
                controllable.append("skipPrevious")

            # If the duration is unknown, disable seeking
            if options["duration"] == "0":
                options.pop("duration")
                options.pop("seekRange")
                controllable.remove("seekTo")

            controllable.append("volume")
            options["volume"] = str(playerManager.get_volume(percent=True) or 0)

            options["controllable"] = ",".join(controllable)
        else:
            if self.last_media_item:
                media_item = self.last_media_item
                options["ratingKey"]         = media_item.get_attr("ratingKey")
                options["key"]               = media_item.get_attr("key")
                options["containerKey"]      = media_item.get_attr("key")
                if media_item.parent.play_queue:
                    options.update(media_item.parent.get_queue_info())
            if player.playback_abort:
                options["state"] = "stopped"
            else:
                options["state"] = "buffering"

        return options


timelineManager = TimelineManager()
