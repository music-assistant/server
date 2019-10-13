#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import sys
import os

from music_assistant import MusicAssistant

if __name__ == "__main__":

    if len(sys.argv) > 0:
        datapath = sys.argv[1]
    else:
         datapath = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) > 1:
        debug = sys.argv[2]
    else:
        debug = False

    MusicAssistant(datapath, debug)
    