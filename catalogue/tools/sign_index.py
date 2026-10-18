#!/usr/bin/env python3
"""Sign the generated index with the real minisign executable and an external key."""
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--minisign",required=True,help="Path to the verified minisign binary.")
    parser.add_argument("--key",required=True,help="External minisign secret-key path; never store it in this repo.")
    parser.add_argument("--public-key",help="Optional public key path used to verify the signature after creation.")
    parser.add_argument("--index",default="dist/catalogue.json")
    parser.add_argument("--signature",default="dist/catalogue.json.minisig")
    args=parser.parse_args()
    binary=Path(args.minisign).resolve()
    key=Path(args.key).resolve()
    index=Path(args.index).resolve()
    signature=Path(args.signature).resolve()
    if not binary.is_file() or not key.is_file() or not index.is_file():
        parser.error("minisign binary, external key, and built index must all exist.")
    signature.parent.mkdir(parents=True,exist_ok=True)
    state=json.loads(Path("state.json").read_text(encoding="utf-8"))
    subprocess.run([str(binary),"-S","-s",str(key),"-m",str(index),"-x",str(signature),
                    "-t",f"Lynewright catalogue serial:{state['serial']}"],check=True)
    if args.public_key:
        subprocess.run([str(binary),"-V","-p",str(Path(args.public_key).resolve()),
                        "-m",str(index),"-x",str(signature)],check=True)
    print(f"Signed {index.name} serial {state['serial']} -> {signature.name}")
    return 0
if __name__=="__main__":
    raise SystemExit(main())
