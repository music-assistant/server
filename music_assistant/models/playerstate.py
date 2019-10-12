#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum

class PlayerState(str, Enum):
    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"
