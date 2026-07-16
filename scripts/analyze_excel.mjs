import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputs = process.argv.slice(2);
if (!inputs.length) throw new Error("Usage: node scripts/analyze_excel.mjs <xlsx>...");

const outputDir = path.resolve(".analysis", "excel");
await fs.mkdir(outputDir, { recursive: true });

const normalize = (value) => {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "bigint") return value.toString();
  return value;
};

const valueType = (value) => {
  if (value === null || value === undefined || value === "") return "empty";
  if (value instanceof Date) return "date";
  return typeof value;
};

const columnName = (number) => {
  let value = Math.max(1, number);
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
};

const results = [];
for (const inputPath of inputs) {
  const absolute = path.resolve(inputPath);
  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(absolute));
  const sheetInspection = await workbook.inspect({
    kind: "sheet",
    include: "id,name",
    maxChars: 20000,
  });
  const sheetLines = sheetInspection.ndjson.trim().split(/\r?\n/).filter(Boolean).map(JSON.parse);
  const sheetNames = sheetLines.map((entry) => entry.name).filter(Boolean);
  const workbookResult = { file: path.basename(absolute), sheets: [] };

  for (let index = 0; index < sheetNames.length; index += 1) {
    const sheetName = sheetNames[index];
    const sheet = workbook.worksheets.getItem(sheetName);
    const used = sheet.getUsedRange(true);
    const values = used ? used.values : [];
    const formulas = used ? used.formulas : [];
    const rowCount = values.length;
    const colCount = values.reduce((max, row) => Math.max(max, row.length), 0);
    const headers = rowCount ? values[0].map(normalize) : [];
    const columns = [];
    for (let col = 0; col < colCount; col += 1) {
      const data = values.slice(1).map((row) => row[col]);
      const nonEmpty = data.filter((value) => value !== null && value !== undefined && value !== "");
      const examples = [...new Map(nonEmpty.slice(0, 50).map((value) => [JSON.stringify(normalize(value)), normalize(value)])).values()].slice(0, 5);
      columns.push({
        column_index: col + 1,
        header: normalize(headers[col] ?? null),
        types: [...new Set(nonEmpty.map(valueType))],
        non_empty_count: nonEmpty.length,
        empty_count: data.length - nonEmpty.length,
        examples,
      });
    }
    const sampleRows = values.slice(0, 8).map((row) => row.map(normalize));
    const formulaCount = formulas.flat().filter((formula) => typeof formula === "string" && formula.startsWith("=")).length;
    workbookResult.sheets.push({ sheet_name: sheetName, row_count: rowCount, column_count: colCount, formula_count: formulaCount, headers, columns, sample_rows: sampleRows });

    if (process.env.SKIP_RENDER !== "1") {
      const safeName = `${path.parse(absolute).name}-${index + 1}-${sheetName}`.replace(/[<>:"/\\|?*]+/g, "_");
      const previewRange = `A1:${columnName(Math.min(colCount || 1, 30))}${Math.min(rowCount || 1, 30)}`;
      const preview = await workbook.render({ sheetName, range: previewRange, scale: 1, format: "png" });
      await fs.writeFile(path.join(outputDir, `${safeName}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
  }
  results.push(workbookResult);
  const resultPath = path.join(outputDir, `${path.parse(absolute).name}.analysis.json`);
  await fs.writeFile(resultPath, JSON.stringify(workbookResult, null, 2), "utf8");
}

await fs.writeFile(path.join(outputDir, "analysis.json"), JSON.stringify(results, null, 2), "utf8");
console.log(JSON.stringify(results, null, 2));
