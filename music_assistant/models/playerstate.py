#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum

class PlayerState(str, Enum):
    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"

    # def from_string(self, string):
    #     if string == "off":
    #         return self.Off
    #     elif string == "stopped":
    #         return self.Stopped
    #     elif string == "paused":
    #         return self.Paused
    #     elif string == "playing":
    #         return self.Playing
