# runtime/bin - bundled command-line programs

Optional drop-in folder for the external programs the launcher's
**Tools** section drives. Nothing is installed here by the project; the
folder exists so that a machine without ffmpeg or 7-Zip on its PATH can
be made to work by copying two files.

| Tool | Program | Windows file | Also accepted |
| --- | --- | --- | --- |
| Review Video Compression | ffmpeg | `ffmpeg.exe` | - |
| Participant ZIP Backup | 7-Zip | `7z.exe` (plus `7z.dll` if your build needs it) | `7zz`, `7za` |

How they are found (`app/tool_binaries.py`):

1. **this folder**, first;
2. **PATH**, as installed system-wide.

`launcher.bat` also prepends this folder to `PATH` for the window it
starts, and `app/tool_binaries.py` prepends it in-process, so anything
those tools start themselves finds the same copy. Both are temporary -
nothing is written to the system PATH or the registry.

Each Tools window shows which copy it is using, or a warning naming this
folder if it found none, and has a **Re-check** button so a program
dropped in here is picked up without restarting the launcher.

The console tools (`tool_compress_review_videos.py`,
`tool_backup_quiz_to_zip.py`) use exactly the same lookup.

Executables placed here are deliberately not committed - see
`.gitignore`.
