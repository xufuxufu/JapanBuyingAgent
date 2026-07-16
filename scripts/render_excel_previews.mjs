import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = path.resolve(process.argv[2]);
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const outputDir = path.resolve(".analysis", "excel", "previews");
await fs.mkdir(outputDir, { recursive: true });
const inspection = await workbook.inspect({ kind: "sheet", include: "name", maxChars: 10000 });
const names = inspection.ndjson.split(/\r?\n/).filter(Boolean).map(JSON.parse).map((x) => x.name).filter(Boolean);
for (const [index, sheetName] of names.entries()) {
  const range = sheetName === "配置" ? "A1:Z10" : "A1:AD15";
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  const safe = `${path.parse(input).name}-${index + 1}-${sheetName}`.replace(/[<>:"/\\|?*]+/g, "_");
  await fs.writeFile(path.join(outputDir, `${safe}.png`), new Uint8Array(await preview.arrayBuffer()));
  console.log(`${sheetName}: ${range}`);
}
