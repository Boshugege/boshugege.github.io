// Components available in every post without an import.
// Add a new one here and it can be used as <Name /> in any .mdx post.
import Compare from "./Compare.astro";
import Figure from "./Figure.astro";
import Note from "./Note.astro";

export const mdxComponents = { Compare, Figure, Note };
