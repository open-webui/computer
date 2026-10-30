<script lang="ts">
	import { onMount } from 'svelte';
	import type { renderToString as katexRenderToString } from 'katex';
	import 'katex/dist/katex.min.css';

	let { text, displayMode = false }: { text: string; displayMode?: boolean } = $props();
	let renderToString = $state<typeof katexRenderToString | null>(null);

	onMount(async () => {
		renderToString = (await import('katex')).renderToString;
	});

	let html = $derived.by(() => {
		try {
			return renderToString?.(text, { displayMode, throwOnError: false, trust: false });
		} catch {
			// Non-parse errors (e.g. excessive nesting) still need an escaped text fallback.
			return null;
		}
	});
</script>

<span class="not-prose" class:display-math={displayMode}>
	{#if html != null}{@html html}{:else}{text}{/if}
</span>

<style>
	.display-math {
		display: block;
		overflow-x: auto;
		max-width: 100%;
	}
</style>
