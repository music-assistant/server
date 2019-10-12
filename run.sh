#!/bin/sh
set -e

SET autoupdate = 'true'

# auto update to latest git version if update environmental variable is set
if [ "$autoupdate" == "true" ]; then
    echo "Auto updating to latest (unstable) git version!"
    cd /tmp
    curl -LOks "https://github.com/marcelveldt/musicassistant/archive/master.zip"
    unzip -q master.zip
    rm -R /usr/src/app
    mkdir /usr/src/app
    cp -rf musicassistant-master/. /usr/src/app
    rm -R /tmp/musicassistant-master
fi

# run program
cd /usr/src/app
exec python3 /usr/src/app/main.py /data > /proc/1/fd/1 2>/proc/1/fd/2