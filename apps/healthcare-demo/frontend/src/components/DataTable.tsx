import type { ChartTable } from "../types";

/**
 * A chart's data as a table: the backend sends the columns and rows with each
 * chart, so the numbers can be read without the image.
 */
export function DataTable({ table, caption }: { table: ChartTable; caption?: string }) {
  if (table.columns.length === 0) return <p className="ds-note">No data yet.</p>;
  return (
    <div className="ds-table-scroll">
      <table className="ds-table">
        {caption && <caption className="visually-hidden">{caption}</caption>}
        <thead>
          <tr>
            {table.columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column} className={typeof cell === "number" ? "num" : undefined}>
                  {cell === null || cell === "" ? "—" : String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
