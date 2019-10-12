#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import sys
import os

from music_assistant import MusicAssistant

if __name__ == "__main__":
    datapath = sys.argv[1]
    if not datapath:
        datapath = os.path.dirname(os.path.abspath(__file__))
    MusicAssistant(datapath)
    