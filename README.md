<p align="center">
  <img src="https://github.com/JanekBln/X-VRAM-Manager/blob/main/images/logo.png" alt="Projekt Logo" width="200">
</p>


X-VRAM Manager v0.31
====================

Standalone XPPython3 texture-pager tuning plugin for X-Plane 12.

OVERVIEW
--------
X-VRAM Manager is a small standalone XPPython3 plugin that adjusts selected
X-Plane texture-pager controls. It does not install a Vulkan layer, does not
inject shaders, does not modify ReShade, and does not replace X-Plane's own
texture pager.

Its purpose is to give X-Plane's existing texture pager a little more operating
room on high-VRAM systems while keeping X-Plane responsible for texture
residency and emergency downscaling.

TESTED CONFIGURATION
--------------------
Development testing was performed with:

- X-Plane 12.4.4-b1 build 124410
- XPPython3 4.7.1
- NVIDIA RTX 3090 24 GB
- ReShade enabled
- MotionVectors/TAA Vulkan layer disabled

The test flight included multiple Python reloads, real-weather activation,
Lossless Scaling usage, cruise, descent, approach and landing.

FILES
-----
PI_XVRAM_Manager.py
    Main XPPython3 plugin.

XVRAM_Manager.ini
    Configuration file.

README.txt
    Installation and usage guide.

CHANGELOG.txt
    Short version history.

LICENSE.txt
    GNU GPL v3 license text.

INSTALLATION
------------
Requirement:

- XPPython3 must already be installed and working.

Copy:

    PI_XVRAM_Manager.py
    XVRAM_Manager.ini

to:

    X-Plane 12\Resources\plugins\PythonPlugins\

Then start X-Plane normally.

VERIFYING THAT IT WORKS
-----------------------
Open X-Plane's Log.txt and search for:

    [X-VRAM]

A normal successful activation should contain lines similar to:

    [X-VRAM] v0.3.0 starting
    [X-VRAM] control: sim/private/controls/tex/paging/max_overdrive original=16
    [X-VRAM] control: sim/private/controls/tex/paging/size_fudge_factor original=1.05
    [X-VRAM] pager fallback controls: ACTIVE (late-resolved)

DEFAULT SETTINGS
----------------
The supplied configuration uses:

    max_overdrive = 64
    size_fudge_factor = 0.75
    downscale_cooldown = 0
    scale_floor = 0

scale_floor = 0 keeps X-Plane's normal emergency texture downscaling available.

HOW IT WORKS
------------
X-Plane already contains its own texture pager. The pager decides which
textures remain resident in GPU memory and when texture resolution must be
reduced to remain inside the available GPU-memory budget.

X-VRAM Manager v0.3 works in three main steps:

1. Late control discovery

   Some X-Plane 12 builds do not expose the private pager controls yet when
   XPPython3 first starts. v0.3 therefore waits and resolves them later from
   the flight loop.

2. Conservative pager tuning

   The default profile sets:

       sim/private/controls/tex/paging/max_overdrive = 64
       sim/private/controls/tex/paging/size_fudge_factor = 0.75

   In simple terms, these values give the pager more room before it reacts
   aggressively and make its texture-size estimate less conservative.

3. Safe restoration

   When the plugin is disabled, stopped or reloaded, it restores the original
   X-Plane values that it captured.

   During testing the observed stock values were:

       max_overdrive = 16
       size_fudge_factor = 1.05

   After a Python reload the plugin starts again, waits for the controls and
   reapplies the configured values.

WHAT IT DOES NOT DO
-------------------
- It does not install a Vulkan layer.
- It does not inject shaders.
- It does not modify ReShade.
- It does not disable X-Plane's texture pager.
- It does not permanently modify X-Plane.exe on disk.
- It does not force all textures to stay at full resolution.
- It does not disable emergency texture downscaling by default.

OPTIONAL BINARY RESERVE PATCH
-----------------------------
The plugin contains an optional in-memory texture-budget reserve patch.

For safety, the patch is only applied when the exact expected machine-code
signature is found exactly once.

If the installed X-Plane build does not match, the plugin logs the mismatch
and skips the patch. It does not guess a memory location.

Example:

    [X-VRAM] budget reserve: known signature hits=0;
    binary reserve patch skipped safely.

On X-Plane 12.4.4-b1 build 124410 this is expected. The normal v0.3 pager
control tuning still works independently.

OPTIONAL SCALE FLOOR
--------------------
scale_floor is disabled by default.

Keep:

    scale_floor = 0

for normal use.

Experimental non-zero values restrict how far X-Plane may reduce texture
resolution under heavy pressure. That can increase out-of-memory risk.

PYTHON RELOADS
--------------
The plugin is designed to behave safely during XPPython3 reloads.

Before unloading it restores the captured stock pager values. After reload it
waits for the controls to become available again and then reapplies the
configured tuning.

A short texture-pager adjustment immediately after a Python reload is normal.

COMMANDS
--------
The plugin registers:

    xvram/apply
        Apply the current configuration.

    xvram/reload_config
        Reload XVRAM_Manager.ini and apply the settings.

    xvram/restore_stock
        Restore the original pager values captured by the plugin.

TROUBLESHOOTING
---------------
If the plugin appears not to work:

1. Open X-Plane\Log.txt
2. Search for:

       [X-VRAM]

3. Confirm that XPPython3 loaded successfully.
4. Confirm that both files are inside:

       X-Plane 12\Resources\plugins\PythonPlugins\

If the log contains:

    pager fallback controls: ACTIVE (late-resolved)

the main v0.3 pager tuning is active.

If the binary reserve patch reports zero signature hits, this does NOT mean
the main plugin failed. It only means that the optional version-sensitive
binary patch was skipped.

COMPATIBILITY NOTES
-------------------
- Designed for X-Plane 12.
- Binary signatures are not intended for X-Plane 11.
- X-Plane private art controls can change between beta versions.
- Re-test after X-Plane updates.
- ReShade can be used independently of this plugin.
- Do not run another plugin that modifies the same texture-pager controls
  during comparison testing.

TEST OBSERVATION
----------------
During the development test flight, v0.3 survived multiple XPPython3 reloads,
real-weather activation, ReShade usage and a complete flight through landing
without the earlier severe VRAM/pager instability seen during previous tests.

This is a tested observation from one system and is not a guarantee for every
aircraft, scenery package or hardware combination.

LICENSE AND CREDITS
-------------------
License: GPL-3.0-or-later.

The texture-pager tuning approach and known binary-signature work were derived
from the open-source Motion-Vectors-for-Xplane-12-V2 project by Vihaan2012 and
contributors, licensed under GPL-3.0-or-later.

X-VRAM Manager separates the pager-tuning part into a standalone XPPython3
plugin so it can be tested without the MotionVectors/TAA Vulkan layer.

DISCLAIMER
----------
Use at your own risk.

This plugin changes undocumented/private X-Plane controls and contains
optional version-sensitive in-memory patch logic. Always re-test it after an
X-Plane update.
