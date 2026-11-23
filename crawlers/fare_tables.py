"""Read rendered table cells with row and column spans expanded."""


def visible_table_rows(page):
    tables = page.locator("table:visible").evaluate_all("""tables =>
        tables.filter(table => !table.querySelector('table')).map(table => {
            const grid = [];
            const rows = Array.from(table.rows).filter(row => row.getClientRects().length);
            for (let r = 0; r < rows.length; r++) {
                grid[r] ??= [];
                let c = 0;
                for (const cell of rows[r].cells) {
                    if (!cell.getClientRects().length) continue;
                    while (grid[r][c] !== undefined) c++;
                    const text = cell.innerText.trim();
                    const end = cell.rowSpan ? Math.min(rows.length, r + cell.rowSpan) : rows.length;
                    for (let i = r; i < end; i++) {
                        grid[i] ??= [];
                        for (let j = c; j < c + cell.colSpan; j++) grid[i][j] = text;
                    }
                    c += cell.colSpan;
                }
            }
            return grid.map(row => Array.from(row, cell => cell ?? ''));
        })""")
    return [tuple(map(tuple, table)) for table in tables]
