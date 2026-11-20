# Android 7.1 for the ProDVX APPC-10DSQPL (hardware version 1.1)

> [!IMPORTANT]
> **Unofficial firmware.** This is not an official ProDVX image. It is something I have put together myself.

**Version:** N1.9.1, the latest stable version.

**Tested on:** APPC-10DSQPL hardware version 1.1, both the PoE/LED model and the model without PoE and LEDs.

## About

ProDVX's official firmware for these tablets only goes up to Android 5.1. I found an Android 7.1 image designed for car head units and, with the help of AI, got it fully working on these tablets. I removed as much of the car software as I could, so the image is as close to standard Android as possible. Being on Android 7.1 also means many more apps can be installed.

I have also added some quality-of-life features. Apart from those, I have kept the image as close as possible to how the official 4.4 and 5.1 images behave.

## Updating

> [!WARNING]
> **Updating to this firmware erases everything on the tablet.**

You can update directly from the official 4.4 or 5.1 firmware. I recommend using the check app, because it confirms that the tablet's hardware version is correct for this image before anything is installed.

1. **Download** the image you want and the check app (`APPC-10DSQ-Check.apk`) from the [latest release](https://github.com/Chrisb003/APPC-10DSQPL-Andriod7.1/releases/latest).
2. Copy the image file (`.img`) to the top level of the SD card and put the card in the tablet.
3. Install the check app on the tablet and open it.
4. The app checks the hardware version and then starts the update from the image on the SD card.

There are two images (`MD5SUMS.txt` in the release lets you check your download):

| Image | Contents |
|---|---|
| `update7.1.V1.1.N1.9.1.Gapps.img` | Standard: Google Play Store, Play services and Chrome |
| `update7.1.V1.1.N1.9.1.NoGapps.img` | No Google apps: no Google software at all, with a simple web browser instead |

## What has changed

The changes fall into two groups:

- **Changes made to adapt the car image:** the work needed to turn an Android 7.1 image built for car head units into firmware that runs these tablets properly and behaves like the official images.
- **Changes compared with the official 4.4 and 5.1 images:** what is new or different for someone moving from the official firmware.

## Changes made to adapt the car image

### Car software removed

- **Car apps removed:** radio, DVD, TV, CAN bus, reversing cameras, the car Bluetooth module's apps, iPod, navigation and factory settings, the car launcher and the car settings screens (GPS, car, amplifier and dialog settings). The car mod's own tools, including its root tool (Magisk), are removed too.
- **Car top bar replaced:** the car's single combined top bar is gone.
- **What stays:** a small background part of the car system remains, because the tablet's front buttons and audio go through it. It now talks to a small stand-in driver in the tablet's kernel instead of car hardware.
- **Standard Android 7.1 screens instead:** Android 7.1's Settings, status bar, navigation bar and home screen (Launcher3). As on the official images, the status bar is at the top and the navigation bar at the bottom.
- **Re-signed system:** the system apps are signed with this project's own keys, so the car versions could be replaced.
- **Last car files removed:** car tools, car touch and input settings, and car-only drivers that nothing on the tablet uses.
- **Smaller system:** the removed car software no longer takes up space.

### Hardware support for this tablet

- **A kernel built for this tablet** (the car image's kernel is for car units), with the correct CPU voltage setting for stable running and the security fixes listed below.
- **Front buttons:** all five work; they never responded on the car image.
- **Volume:** controls the tablet's own audio chip instead of a car amplifier.
- **Headphone jack:** separate speaker and headphone outputs, and plug detection.
- **Bluetooth:** the car image used an external car Bluetooth module, so Android's own Bluetooth was switched off. It now works with the tablet's built-in chip: pairing, and music to Bluetooth speakers and headphones.
- **Wi-Fi:** uses the tablet's AP6xxx chip; the car's Realtek drivers are removed.
- **Tilt sensor:** the car image reported no sensors; the official 5.1 sensor driver is used instead, so auto-rotate works.
- **Auto-rotate:** the car image always forced landscape.
- **Side light bar and rear power LED** work (the power LED turns green when the tablet has started).
- **USB-A port:** now powered (it had no power on the car image).
- **SD card:** shown as "SD card" (the car image called it "GPS", its map card).
- **Display:** brightness no longer drops to minimum, the lowest setting no longer turns the backlight off, and tablets with a different screen panel display correctly.
- **Memory speed:** 456 MHz, as on the official 5.1 image.
- **HDMI out:** 1080p by default.

### Security

- **Security patch level: 5 May 2017.** This is the newest level possible for this image. The Android 7.1 system it is built on is only available as a finished image, without its source code, so Google's later Android patches cannot be applied to it. The parts that can be updated, the kernel and the WebView, have been.
- **Kernel fixes for known root exploits.** The tablet's kernel (Linux 3.0) had six known holes that let any app take full control of the tablet. All six are fixed:

  | Issue | Known as |
  |---|---|
  | CVE-2016-5195 | "Dirty COW" (memory copy-on-write) |
  | CVE-2014-3153 | "Towelroot" (futex) |
  | CVE-2015-1805 | Pipe read/write overflow |
  | CVE-2015-3636 | "PingPong root" (ping sockets) |
  | CVE-2014-2851 | Ping socket reference count |
  | CVE-2014-0196 | Terminal (n_tty) write race |

- **Root removed for apps:** the car image came with a root tool (Magisk) and car tools that ran scripts as root at every start-up. Both are removed, so no app can get root. As on the official 5.1 image, root over adb is still available, but only to computers approved on the tablet.
- **adb approval restored:** the car image accepted any computer over USB without asking. Each computer must now be approved on the tablet, as on the official images, and new computers can be refused automatically (see Remote management).
- **Newer WebView:** web pages shown inside apps use Bromite 108 instead of the car image's WebView 55, which has many known security holes.
- **Remote management is off by default.**

### Made to match the official images

- **Identity:** model APPC-10DSQ, manufacturer ProDVX, the factory serial number (also shown over USB) and the factory Ethernet, Wi-Fi and Bluetooth addresses, instead of the car image's generic "px3" identity. Device name APPC-10DSQ.
- **Defaults like the official images:** screen never sleeps, installing apps from unknown sources allowed, Wi-Fi, Bluetooth and touch sounds off, no lock screen, battery indicator hidden, language English (UK) and time zone London.
- **Network debugging (adb)** on port 5555 with each computer approved on the tablet, as on the official images. The car image had adb over USB only, with no approval.
- **Power menu:** Restart and Power off (the car image had "Recovery mode" and "Lock now").
- **Tablet wording:** no SIM, IMEI or phone entries, and no "System updates" entries (they pointed at Google's update checker, which has no updates for this tablet).
- **Fast Settings:** the Settings list appears straight away instead of waiting for the search index (up to 30 seconds on a slow tablet).

## Changes compared with the official 4.4 and 5.1 images

### Android and apps

- **Android 7.1.2**, up from 4.4.4 (official 2018 image) and 5.1.1 (official 2020 image), so many more apps can be installed.
- **Two images:** standard with Google Play, Play services and Chrome, or no Google software at all, with a simple web browser.
- **Newer WebView** (the component apps use to show web pages): Bromite 108 (Chromium 108) in both images. The official 4.4 image has Chromium 30. On the Google image, if the Play Store later updates Chrome past version 108, the tablet automatically switches to Chrome as the WebView.

### Hardware

Everything that works on the official images still works:

- display, touch and brightness;
- Ethernet, Wi-Fi and Bluetooth;
- speaker, headphone jack and microphone;
- tilt sensor and auto-rotate;
- HDMI out;
- SD card, the USB-A port and micro-USB (host or device);
- real-time clock;
- side light bar and rear power LED;
- all five front buttons.

### Installing future updates

- **Settings › Firmware update** checks an image on the SD card or a USB stick (chip, complete file and hardware version), then installs it.
- Updates can also be installed from the tablet's own recovery menu (tested on 5.1).

### Panel settings

The extra Settings screens from the official images are carried over and extended:

- **Ethernet:** DHCP or static IP.
- **HDMI:** on/off, resolution and scaling.
- **USB mode:** automatic, host or device.
- **Physical buttons** (the official images have one on/off switch):
  - all buttons on or off;
  - each button can be remapped (Up, Down, Vol+, Vol− and Back), with separate actions for a tap, a 1–3 second hold and a 3 second hold;
  - actions include opening an app, "Do nothing" and Unpin;
  - a Back press while the screen is off can wake it.
- **Power schedule** (the official images have screen on/off times only):
  - weekly times, optionally from or until a date, one-off dates and days off;
  - screen on, screen off and restart;
  - a 14-day preview.
- **Quiet hours** (new): between a "screen off" and a "screen on" time, the screen turns itself off again if someone wakes it. After a touch it stays on for a chosen time (15 seconds to 12 hours, or until the next scheduled time).
- **Start-up app** (new): opens an app at start-up, optionally after a delay. It can **pin** the app (kiosk mode):
  - permission to pin is asked only once per app;
  - **unpin lock:** a PIN is needed to leave the pinned app;
  - holding a front button mapped to Back leaves a pinned app, like the on-screen Back button;
  - **a pinned app that crashes is restarted** (closing it or force-stopping it doesn't count as a crash).
- **Date and time:** clock, time zone and network time.
- **Bluetooth list:** unnamed nearby devices are grouped under one "Show unnamed devices" row.

### Remote management

> [!NOTE]
> **Remote management only works with tablets on the same network as the computer managing them.** The dashboard and network debugging connect to each tablet directly over the local network; they cannot reach tablets over the internet. To manage tablets at another site, that computer must be on the site's network, for example through a VPN that connects the two networks.

- **Network debugging (adb) is on by default** on port 5555, as on the official images, and each computer must be approved on the tablet the first time. New: it can be switched off in Developer options › Network debugging.
- **"Allow new computers to ask"** switches for network and USB, and a list of refused computers. The "USB debugging connected" notification can be turned off.
- **Remote management** switch, **off by default**. The web dashboard needs it on; while it is off, a dashboard can only see whether the tablet is online.
- **Hide Developer options:** removes the menu from Settings but keeps every choice in it active.

### Web dashboard (new)

Runs with Python on a Windows PC, Mac, Linux computer or Raspberry Pi, and is used from a web browser. Like the rest of remote management, it only reaches tablets on the same network as the computer it runs on.

**Get it:** download `APPC-Tablet-Dashboard-Python-0.4.zip` from the [dashboard release](https://github.com/Chrisb003/APPC-10DSQPL-Andriod7.1/releases/tag/dashboard-0.4). Install and use instructions for Windows, Mac and Linux are in the [dashboard folder](APPC-Tablet-Dashboard-Python-0.4/).

- **Tablets:**
  - status and screenshots;
  - files on internal storage and the SD card;
  - installing apps (APKs);
  - firmware updates for several tablets at once;
  - power schedules and quiet hours;
  - start-up app, pinning and the unpin PIN;
  - buttons, volume, brightness and clock;
  - copying settings between tablets;
  - flashing the side LEDs to find a tablet.
- **USB set-up:** one click prepares a tablet plugged in over USB for management over the network.
- **Server:**
  - login, and HTTPS with its own certificate;
  - changeable port, moving to the next free port if the chosen one is in use.
- **Progress bars** for uploads and app installs.

### Panel test app (new)

Pass/fail tests, with a history, for:

- display, touch and backlight;
- LEDs and front buttons;
- speaker, headphones and microphone;
- tilt sensor;
- Ethernet, Wi-Fi and Bluetooth;
- USB and SD storage;
- HDMI and the clock.

## Not supported

- **Camera, GPS and NFC:** the same as the official images. No camera is fitted, and GPS and NFC are not used on these tablets.
- **Bluetooth calls (headsets):** not supported. Music over Bluetooth works.
- **Apps that need Android 8 or newer, or a 64-bit processor,** cannot be installed.
