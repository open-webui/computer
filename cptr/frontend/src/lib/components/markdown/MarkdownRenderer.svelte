<script lang="ts">
	/**
	 * General-purpose Markdown renderer.
	 *
	 * Usage:
	 *   <MarkdownRenderer content={markdownString} />
	 *
	 * Parses markdown via marked.lexer() into AST tokens,
	 * then renders each token as a Svelte component.
	 * Styled entirely via Tailwind prose. No custom CSS.
	 */

	import { mathMarkdown } from './math';
	import BlockRenderer from './BlockRenderer.svelte';

	interface Props {
		content: string;
	}

	let { content }: Props = $props();

	// Pre-process wikilinks: [[target|label]] → <wikilink target="target">label</wikilink>
	const WIKILINK_RE = /\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]/g;

	function preprocessWikilinks(text: string): string {
		return text.replace(WIKILINK_RE, (_match, target, label) => {
			const t = target.trim();
			const l = (label || target).trim();
			return `<wikilink data-target="${t}">${l}</wikilink>`;
		});
	}

	let tokens = $derived.by(() => {
		if (!content) return [];
		try {
			const processed = preprocessWikilinks(content);
			return mathMarkdown.lexer(processed);
		} catch {
			return [];
		}
	});
</script>

<div
	class="prose prose-sm dark:prose-invert max-w-none break-words leading-relaxed prose-p:my-2 prose-p:leading-relaxed prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold prose-headings:leading-snug prose-strong:font-semibold prose-code:before:content-none prose-code:after:content-none prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-pre:my-3 prose-blockquote:my-3 prose-hr:my-4 prose-img:my-2 [&>:first-child]:mt-0 [&>:last-child]:mb-0"
>
	<BlockRenderer {tokens} />
</div>
