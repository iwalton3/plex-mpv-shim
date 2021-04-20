#!/bin/bash
# This script:
# - Download/updates default-shader-pack

cd "$(dirname "$0")"

function download_compat {
    if [[ "$AZ_CACHE" != "" ]]
    then
        download_id=$(echo "$2" | md5sum | sed 's/ .*//g')
        if [[ -e "$AZ_CACHE/$3/$download_id" ]]
        then
            echo "Cache hit: $AZ_CACHE/$3/$download_id"
            cp "$AZ_CACHE/$3/$download_id" "$1"
            return
        elif [[ "$3" != "" ]]
        then
            rm -r "$AZ_CACHE/$3" 2> /dev/null
        fi
    fi
    if [[ "$(which wget 2>/dev/null)" != "" ]]
    then
        wget -qO "$1" "$2"
    else [[ "$(which curl)" != "" ]]
        curl -sL "$2" > "$1"
    fi
    if [[ "$AZ_CACHE" != "" ]]
    then
        echo "Saving to: $AZ_CACHE/$3/$download_id"
        mkdir -p "$AZ_CACHE/$3/"
        cp "$1" "$AZ_CACHE/$3/$download_id"
    fi
}

function get_resource_version {
    curl -s --head https://github.com/"$1"/releases/latest | \
        grep -i '^location: ' | sed 's/.*tag\///g' | tr -d '\r'
}

if [[ "$1" == "--get-pyinstaller" ]]
then
    echo "Downloading pyinstaller..."
    pi_version=$(get_resource_version pyinstaller/pyinstaller)
    download_compat release.zip "https://github.com/pyinstaller/pyinstaller/archive/$pi_version.zip" "pi"
    (
        mkdir pyinstaller
        cd pyinstaller
        unzip ../release.zip > /dev/null && rm ../release.zip
        mv pyinstaller-*/* ./
        rm -r pyinstaller-*
    )
    exit 0
elif [[ "$1" == "--gen-fingerprint" ]]
then
    (
        get_resource_version pyinstaller/pyinstaller
        get_resource_version iwalton3/default-shader-pack
    ) | tee az-cache-fingerprint.list
    exit 0
fi

# Download default-shader-pack
update_shader_pack="no"
if [[ ! -e "plex_mpv_shim/default_shader_pack" ]]
then
    update_shader_pack="yes"
elif [[ -e ".last_sp_version" ]]
then
    if [[ "$(get_resource_version iwalton3/default-shader-pack)" != "$(cat .last_sp_version)" ]]
    then
        update_shader_pack="yes"
    fi
fi

if [[ "$update_shader_pack" == "yes" ]]
then
    echo "Downloading shaders..."
    sp_version=$(get_resource_version iwalton3/default-shader-pack)
    download_compat release.zip "https://github.com/iwalton3/default-shader-pack/archive/$sp_version.zip" "sp"
    rm -r plex_mpv_shim/default_shader_pack 2> /dev/null
    (
        mkdir default_shader_pack
        cd default_shader_pack
        unzip ../release.zip > /dev/null && rm ../release.zip
        mv default-shader-pack-*/* ./
        rm -r default-shader-pack-*
    )
    mv default_shader_pack plex_mpv_shim/
    echo "$sp_version" > .last_sp_version
fi

# Generate package
if [[ "$1" == "--install" ]]
then
    if [[ "$(which sudo 2> /dev/null)" != "" && ! "$*" =~ "--local" ]]
    then
        sudo pip3 install .[all]
    else
        pip3 install .[all]
    fi

elif [[ "$1" != "--skip-build" ]]
then
    rm -r build/ dist/ .eggs 2> /dev/null
    mkdir build/ dist/
    echo "Building release package."
    python3 setup.py sdist bdist_wheel > /dev/null
fi

