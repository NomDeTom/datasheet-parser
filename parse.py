"""
Datasheet parser entry point.
Usage: python parse.py <pdf_path> [--format json|csv] [--all] [--debug]
"""
import sys
import click
from pathlib import Path

# Ensure Unicode characters (µ, ×, –, °, …) survive on Windows consoles
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from extractor.i2c_registers import I2CRegisterExtractor
from extractor.elec_chars import ElecCharExtractor
from extractor.device_info import DeviceInfoExtractor
from extractor.generic import GenericExtractor
from extractor.output import write_registers, write_elec_chars, write_device_info

# TI document IDs always start with SL (SLVSCE0, SLUSDV2B, …)
_TI_DOC_RE = __import__("re").compile(r"^SL[A-Z]{2,}", __import__("re").IGNORECASE)


@click.command()
@click.argument("pdf_path", required=False)
@click.option("--all", "parse_all", is_flag=True, help="Parse all PDFs in datasheets/")
@click.option("--format", "fmt", default="json",
              type=click.Choice(["json", "csv"]), show_default=True)
@click.option("--registers-only", is_flag=True, help="Skip electrical characteristics")
@click.option("--elec-only", is_flag=True, help="Skip register extraction")
@click.option("--debug", is_flag=True, help="Print intermediate extraction details")
@click.option("--generic", "force_generic", is_flag=True,
              help="Force generic (non-TI) extractor for device info")
def main(pdf_path, parse_all, fmt, registers_only, elec_only, debug, force_generic):
    input_dir = Path("datasheets")
    output_dir = Path("output")

    if parse_all:
        pdfs = list(input_dir.glob("*.pdf"))
        if not pdfs:
            click.echo("No PDFs found in datasheets/")
            return
    elif pdf_path:
        pdfs = [Path(pdf_path)]
    else:
        click.echo("Provide a PDF path or use --all")
        raise SystemExit(1)

    for pdf in pdfs:
        click.echo(f"\nParsing {pdf.name} ...")
        dest = output_dir / pdf.stem

        # ── Device Info ────────────────────────────────────────────────────
        di_extractor = DeviceInfoExtractor(pdf, debug=debug)
        device_info = di_extractor.extract()

        # Fall back to generic extractor if this doesn't look like a TI datasheet
        # (TI doc IDs always start with SL; no doc_id means TI extraction also failed)
        if force_generic or not _TI_DOC_RE.match(device_info.document_id):
            if debug:
                click.echo(f"  [generic fallback] doc_id={device_info.document_id!r}")
            device_info = GenericExtractor(pdf, debug=debug).extract()
        write_device_info(device_info, dest)
        pkgs = ", ".join(
            f"{p.package_type}({p.pins or '?'}) {p.body_size}"
            for p in device_info.packages
        )
        click.echo(f"  Device    : {device_info.title[:70]}")
        click.echo(f"  Package   : {pkgs}")
        specs_summary = " | ".join(filter(None, [
            f"VIN {device_info.vin_min}–{device_info.vin_max}" if device_info.vin_min else "",
            f"startup {device_info.vin_startup}" if device_info.vin_startup else "",
            f"VOUT {device_info.vout_min}–{device_info.vout_max}" if device_info.vout_min else "",
            f"VS {device_info.vsupply_min}–{device_info.vsupply_max}" if device_info.vsupply_min else "",
            f"IMAX {device_info.iout_max}" if device_info.iout_max else "",
            f"freq {device_info.freq_max}" if device_info.freq_max else "",
        ]))
        if specs_summary:
            click.echo(f"  Key specs : {specs_summary}")

        # ── I2C Registers ──────────────────────────────────────────────────
        if not elec_only:
            reg_extractor = I2CRegisterExtractor(pdf, debug=debug)
            registers = reg_extractor.extract()
            if registers:
                click.echo(f"  Registers : {len(registers)} found")
                write_registers(registers, dest, fmt)
            else:
                click.echo(f"  Registers : none found")

        # ── Electrical Characteristics ──────────────────────────────────────
        if not registers_only:
            ec_extractor = ElecCharExtractor(pdf, debug=debug)
            sections = ec_extractor.extract()
            if sections:
                total_specs = sum(len(s.specs) for s in sections)
                click.echo(f"  Elec chars: {len(sections)} section(s), "
                           f"{total_specs} spec(s)")
                for s in sections:
                    click.echo(f"    [{s.source_page:3d}] {s.name} — {len(s.specs)} spec(s)")
                write_elec_chars(sections, dest, fmt)
            else:
                click.echo(f"  Elec chars: none found")

        click.echo(f"  Written to {dest}/")


if __name__ == "__main__":
    main()
