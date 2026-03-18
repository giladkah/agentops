"""
py2app setup — packages ensemble_menubar.py into a standalone .app bundle.

Usage:
    pip install py2app
    python setup_menubar.py py2app

Output: dist/Ensemble.app
"""
from setuptools import setup

APP = ["ensemble_menubar.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Ensemble",
        "CFBundleDisplayName": "Ensemble",
        "CFBundleIdentifier": "dev.ensemblecode.app",
        "CFBundleVersion": "0.3.0",
        "CFBundleShortVersionString": "0.3.0",
        "LSUIElement": True,          # Hides from Dock — menubar only
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
    "packages": ["rumps", "requests", "flask", "sqlalchemy", "anthropic"],
    "excludes": ["tkinter", "test", "unittest"],
    "iconfile": "icon.icns",          # Add your .icns file here
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    name="Ensemble",
)
