# UI design

Four screens for the tool, as a clickable prototype. Nothing here is wired to the pipeline: it is the design, not an implementation.

Canvas: https://claude.ai/artifact/4pSZMHuUXPjzcKP277LH3a (private; share it from the page's Share menu before sending it on)

| File | Screen | What it is for |
| --- | --- | --- |
| `Main.dc.html` | Run results | The landing screen: counts, the extracted figures with their source document and page, and panels for what needs review and what was not read |
| `NewRun.dc.html` | Start a run | Choose the data room, the figures to extract and the accepted currencies, with the time and cost shown before committing |
| `Verify.dc.html` | Verify a figure | Page text on the left with the cited lines highlighted, figures on the right to confirm or reject. Records who checked what |
| `Runs.dc.html` | Run history | The run log as a table, including failed runs |
| `canvas.json` | — | Layout index: which artboard sits where on the canvas |

These are Design Component files (`.dc.html`): self-contained HTML that renders in the Claude design canvas. They open in a browser as plain HTML too, though the `{{…}}` placeholders will not fill in.

## What the design is answering

- **Figures carry their source.** Every row names a document and page, and links to the page, because the pilot partners would not sign off on figures they could not check.
- **Review and skipped are on the main screen**, not buried, since a quiet failure is the known risk.
- **Documents read but not flagged are listed.** All three pilot agents found a document that simply vanished from the report.
- **`NOT STATED` is shown as a label**, with a line saying the tool does not infer a currency.
- **The model picker states that page text leaves the firm**, which is the Topic 5 trust boundary.

## Look

Muted navy (`#16243A` ink, `#2E4A6B` accent) on white, with `#F4F6F9` and `#EBEFF5` for grounds and `#DCE3EC` for borders. Inter throughout, IBM Plex Mono for page text on the verification screen. Colour carries meaning only: navy normal, muted amber for attention, muted red for a stopped run. Those three differ in lightness as well as hue, so they do not rely on colour vision.

## Data in the screens

The figures, pages, counts, timings and costs on the first three screens are the real Northwind sample run of 23 September 2026. The run history keeps those two logged runs and adds three plausible rows so the warning and failure states are visible; no logged run has failed.
