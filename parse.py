"""
Datasheet parser entry point.
Usage: python parse.py <pdf_path> [--format json|csv] [--all] [--debug]
"""
import click
from pathlib import Path

from extractor.i2c_registers import I2CRegisterExtractor
from extractor.elec_chars import ElecCharExtractor
from extractor.output import write_registers, write_elec_chars


@click.command()
@click.argument("pdf_path", required=False)
@click.option("--all", "parse_all", is_flag=True, help="Parse all PDFs in datasheets/")
@click.option("--format", "fmt", default="json",
              type=click.Choice(["json", "csv"]), show_default=True)
@click.option("--registers-only", is_flag=True, help="Skip electrical characteristics")
@click.option("--elec-only", is_flag=True, help="Skip register extraction")
@click.option("--debug", is_flag=True, help="Print intermediate extraction details")
def main(pdf_path, parse_all, fmt, registers_only, elec_only, debug):
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
