# Bundled CMSIS device pack

`Renesas.RA_DFP.slim.pack` is a **trimmed** copy of Renesas' `Renesas.RA_DFP`
device family pack (source version **6.4.0**). It defines the `r7fa4m1ab`
target (Arduino UNO R4 Minima MCU) and its flash algorithms so pyOCD can flash
the RA4M1 **offline**, without the device pack first being downloaded into the
user's `~/.pyocd` / cmsis-pack-manager cache via `pyocd pack install`.

The build bundles it (`esp-prog2-flasher.spec` → `datas`) and `flasher.py`
passes it to pyOCD (`_open_session` → `options["pack"]`).

## Why slim

The full `Renesas.RA_DFP` pack is ~88 MB — almost entirely per-device `SVD`
register-description files that flashing never touches. Since the binary is a
PyInstaller **one-file** build (re-extracted to a temp dir on every launch),
bundling the full pack would add ~88 MB to each launch. The slim pack keeps
only what `r7fa4m1ab` needs to flash: the `.pdsc` and the three referenced
flash algorithms (~47 KB total).

## Regenerating (when the source pack version changes)

Get the full pack (`pyocd pack install r7fa4m1ab` populates the
cmsis-pack-manager cache with `Renesas/RA_DFP/<version>.pack`), then keep only
the pdsc + the RA4M1 flash algorithms:

```sh
FULL=".../cmsis-pack-manager/Renesas/RA_DFP/<version>.pack"
mkdir slim && cd slim
unzip "$FULL" \
  "Renesas.RA_DFP.pdsc" \
  "Flash/RA4M1_256K.FLM" "Flash/RA4M1_DATA.FLM" "Flash/RA4M1_CONF.FLM"
zip -r ../Renesas.RA_DFP.slim.pack .
```

The three `RA4M1_*.FLM` names come from the `<algorithm .../>` entries under
the `R7FA4M1AB` device in `Renesas.RA_DFP.pdsc`. If a future pack renames them,
update the list above and the extract command.
