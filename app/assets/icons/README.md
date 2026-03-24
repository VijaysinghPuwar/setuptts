# App Icons

Place your icon files here:

| File       | Format | Size        | Used by                      |
|------------|--------|-------------|------------------------------|
| app.icns   | ICNS   | 1024×1024   | macOS .app bundle & DMG      |
| app.ico    | ICO    | 256×256     | Windows EXE & installer      |
| app.png    | PNG    | 1024×1024   | Fallback / Linux              |

## Generating icons from a PNG

If you have a 1024×1024 PNG source:

### macOS ICNS (requires iconutil, built into macOS)

```bash
mkdir icon.iconset
sips -z 16 16     app.png --out icon.iconset/icon_16x16.png
sips -z 32 32     app.png --out icon.iconset/icon_16x16@2x.png
sips -z 32 32     app.png --out icon.iconset/icon_32x32.png
sips -z 64 64     app.png --out icon.iconset/icon_32x32@2x.png
sips -z 128 128   app.png --out icon.iconset/icon_128x128.png
sips -z 256 256   app.png --out icon.iconset/icon_128x128@2x.png
sips -z 256 256   app.png --out icon.iconset/icon_256x256.png
sips -z 512 512   app.png --out icon.iconset/icon_256x256@2x.png
sips -z 512 512   app.png --out icon.iconset/icon_512x512.png
cp app.png              icon.iconset/icon_512x512@2x.png
iconutil -c icns icon.iconset -o app.icns
rm -rf icon.iconset
```

### Windows ICO (requires ImageMagick)

```bash
convert app.png -resize 256x256 app.ico
```

## Note
If no icon files are present, SetupTTS renders its icon
programmatically at runtime and still looks great.
