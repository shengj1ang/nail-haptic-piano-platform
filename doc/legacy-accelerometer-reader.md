# read_data_from_accelerometer - legacy LIS3DH reader

Early standalone accelerometer tooling, kept as reference. These files
predate the unified haptic-piano firmware (`teensy_driver/`): the
bundled sketch bit-bangs SPI to a single LIS3DH and prints bare
`x,y,z` lines, and the two Python scripts parse exactly that format.

| File | Purpose |
|---|---|
| `read_data_from_accelerometer.ino` | Old standalone Teensy sketch: software-SPI LIS3DH readout, prints `x,y,z` per sample. Superseded by `teensy_driver/accel_driver.cpp`. |
| `plot_lis3dh.py` | Live matplotlib X/Y/Z plot of those `x,y,z` lines. |
| `read_serial_live.py` | Raw serial line printer (debug). |

**For the current rig, use the launcher instead**: section
"2. Feature Testing" → **Accelerometer Live View**
(`app/gui/accelerometer_window.py`). It speaks the current firmware
protocol (`A START <ms>` / `ACC,<id>,x,y,z`, firmware >= v2.5.0),
lets you pick which sensor id to follow (persisted to `config.json`
as `accelerometer.sensor_id`, also the default sensor in the
validation sweep windows), and runs the serial I/O off the GUI thread.

The scripts here only work against hardware flashed with the old
sketch - they will show nothing when pointed at the current firmware,
which streams in the `ACC,<id>,...` format and only after an
`A START` command.
