#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import sys
import os
import logging
from aiorun import run
import asyncio
import uvloop

logger = logging.getLogger()
logformat = logging.Formatter('%(asctime)-15s %(levelname)-5s %(name)s.%(module)s -- %(message)s')
consolehandler = logging.StreamHandler()
consolehandler.setFormatter(logformat)
logger.addHandler(consolehandler)


def get_config():
    ''' start config handling '''
    data_dir = ''
    debug = False
    update_latest = False
    # prefer command line args
    if len(sys.argv) > 1:
        data_dir = sys.argv[1]
    if len(sys.argv) > 2:
        debug = sys.argv[2] == "debug"
    if len(sys.argv) > 3:
        update_latest = sys.argv[3] == "update"
    # fall back to environment variables (for plain docker)
    if os.environ.get('mass_datadir'):
        data_dir = os.environ['mass_datadir']
    if os.environ.get('mass_debug'):
        debug = os.environ['mass_debug'].lower() != 'false'
    if os.environ.get('mass_update'):
        update_latest = os.environ['mass_update'].lower() != 'false'
    # hassio config file found
    conf_file = '/data/options.json'
    if os.path.isfile(conf_file):
        try:
            import json
            with open(conf_file) as f:
                conf = json.loads(f.read())
                data_dir = conf['data_dir']
                debug = conf['debug_messages']
                update_latest = conf['use_nightly']
        except:
            logger.exception('could not load options.json')
    return data_dir, debug, update_latest

def do_update():
    ''' auto update to latest git version '''
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(".git") or os.path.isdir("%s/.git" % base_dir):
        # dev environment
        return
    logger.info("Updating to latest Git version!")
    import subprocess
    # TODO: handle this properly
    args = """
        cd %s
        curl -LOks "https://github.com/marcelveldt/musicassistant/archive/master.zip"
        unzip -q master.zip
        pip install -r musicassistant-master/requirements.txt
        cp -rf musicassistant-master/music_assistant .
        cp -rf musicassistant-master/mass.py .
        rm -R musicassistant-master
    """ % (base_dir, )
    if subprocess.call(args, shell=True) == 0:
        logger.info("Update succesfull")
    else:
        logger.error("Update failed - do you have curl and zip installed ?")


if __name__ == "__main__":
    # get config
    data_dir, debug, update_latest = get_config()
    if update_latest:
        do_update()
    # create event_loop with uvloop
    event_loop = asyncio.get_event_loop()
    uvloop.install()
    # config debug settings if needed
    if debug:
        event_loop.set_debug(True)
        logger.setLevel(logging.DEBUG)
        logging.getLogger('aiosqlite').setLevel(logging.INFO)
        logging.getLogger('asyncio').setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)
    # start music assistant!
    from music_assistant import MusicAssistant
    mass = MusicAssistant(data_dir, event_loop)
    run(mass.start(), loop=event_loop)
    