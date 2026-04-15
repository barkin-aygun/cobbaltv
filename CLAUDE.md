# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**bloodcam** is a two part project. One part is a script that runs in a Raspberry Pi Zero 2 W with a 12MP wide angle camera, and takes and uploads pictures every few seconds to the other part.

Other part is a Discord bot running in a server that will create interactive galleries of these pictures in a single Discord message that people can use to view the game and browse through the photos.

### Core Capabilities

- **Camera capture**: Takes regular photos/snapshots from an attached RPi camera on a schedule
- **Upload to Discord bot**: Should trigger an update for the Discord bot to add the latest picture to it's current stack.


### Tech Stack

- **Language**: Python
- **Platform**: Raspberry Pi W (limited CPU/RAM — keep dependencies light)
- **Camera**: RPi camera module (picamera2 / libcamera)
- **Discord**: Bot for the interactive messages.
