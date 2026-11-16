"""Fail before building when inputs for the four client products are unavailable."""
import argparse
import json
from pathlib import Path
from build_release import load_enrollment

parser=argparse.ArgumentParser()
parser.add_argument('--uv',type=Path,required=True)
parser.add_argument('--enrollment-file',type=Path,required=True)
args=parser.parse_args()
if not args.uv.is_file():raise SystemExit('Linux uv is missing; set LINUX_UV to a Linux amd64 uv executable')
with args.uv.open('rb') as stream:header=stream.read(20)
if header[:4]!=b'\x7fELF' or header[18:20]!=b'\x3e\x00':raise SystemExit('LINUX_UV must be Linux amd64 ELF')
if not args.enrollment_file.is_file():raise SystemExit('Private enrollment is missing; set ENROLLMENT_FILE to your protected enrollment JSON')
try:
    load_enrollment(args.enrollment_file)
except ValueError as exc:
    raise SystemExit(str(exc)) from None
print('Linux runtime and private enrollment inputs validated; credential contents are not displayed')
