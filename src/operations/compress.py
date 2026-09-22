"""PDF compression via Ghostscript (unchanged settings from the working backend).

  gs -sDEVICE=pdfwrite -dCompatibilityLevel=1.4 -dPDFSETTINGS=/ebook ...

Future tools (merge/split/rotate/...) belong in sibling modules here, e.g.
operations/merge.py, sharing common/ helpers. Nothing else should shell out.
"""
import os
import subprocess


def ghostscript_bin():
    return os.environ.get("GHOSTSCRIPT_BIN", "gs")


def compress_pdf(input_file, output_file):
    command = [
        ghostscript_bin(),
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dPDFSETTINGS=/ebook",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-sOutputFile=%s" % output_file,
        input_file,
    ]
    subprocess.run(command, check=True)
