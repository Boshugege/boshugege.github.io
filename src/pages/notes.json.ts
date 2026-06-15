import { getNotes } from "../lib/notes";

export async function GET() {
  const notes = await getNotes();
  return Response.json({
    version: 1,
    updatedAt: new Date().toISOString(),
    entries: notes.map(({ date, title, body, html }) => ({
      date,
      title,
      summary: body.replace(/[`*_#>]/g, "").replace(/\s+/g, " ").slice(0, 220),
      contentHtml: html,
    })),
  });
}
