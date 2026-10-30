<script lang="ts">
	import type { Token } from 'marked';
	import KatexRenderer from './KatexRenderer.svelte';
	import { openFileTab, setFileBrowserCwd, setActiveTab } from '$lib/stores';
	import { t } from '$lib/i18n';

	interface Props {
		items: Token[];
	}

	let { items }: Props = $props();

	let decoder: HTMLTextAreaElement | undefined;
	function decodeEntities(text: string): string {
		if (typeof document === 'undefined') return text;
		if (!text.includes('&')) return text;
		if (!decoder) decoder = document.createElement('textarea');
		decoder.innerHTML = text;
		return decoder.value;
	}

	const WIKILINK_HTML_RE = /^<wikilink data-target="([^"]+)">([^<]+)<\/wikilink>$/;

	function parseWikilink(raw: string): { target: string; label: string } | null {
		const match = raw.trim().match(WIKILINK_HTML_RE);
		if (match) return { target: match[1], label: match[2] };
		return null;
	}
</script>

{#each items as item}
	{#if item.type === 'mathInline'}
		<KatexRenderer text={item.text} displayMode={item.displayMode} />
	{:else if item.type === 'text'}
		{#if 'tokens' in item && item.tokens}
			<svelte:self items={item.tokens} />
		{:else}
			{decodeEntities('text' in item ? item.text : item.raw)}
		{/if}
	{:else if item.type === 'strong'}
		<strong
			>{#if 'tokens' in item && item.tokens}<svelte:self
					items={item.tokens}
				/>{:else}{item.raw}{/if}</strong
		>
	{:else if item.type === 'em'}
		<em
			>{#if 'tokens' in item && item.tokens}<svelte:self
					items={item.tokens}
				/>{:else}{item.raw}{/if}</em
		>
	{:else if item.type === 'del'}
		<del
			>{#if 'tokens' in item && item.tokens}<svelte:self
					items={item.tokens}
				/>{:else}{item.raw}{/if}</del
		>
	{:else if item.type === 'codespan'}
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<code
			class="codespan cursor-pointer"
			onclick={() => {
				const text = 'text' in item ? item.text : item.raw;
				navigator.clipboard.writeText(text);
			}}>{'text' in item ? item.text : item.raw}</code
		>
	{:else if item.type === 'link'}
		{@const href = 'href' in item ? item.href : ''}
		{#if href?.startsWith('file:///')}
			{@const rawPath = decodeURIComponent(href.replace('file://', ''))}
			{@const isDirectory = rawPath.endsWith('/')}
			{@const filePath = isDirectory ? rawPath.slice(0, -1) : rawPath}
			{@const fileName = filePath.split('/').pop() || filePath}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<button
				class="inline-flex items-center gap-1 px-1 py-px rounded text-[0.8125rem] leading-snug font-medium cursor-pointer border-none text-blue-500 dark:text-blue-400 hover:bg-blue-500/8 transition-colors align-baseline"
				title={filePath}
				onclick={(e) => {
					e.preventDefault();
					if (isDirectory) {
						setFileBrowserCwd(filePath);
						setActiveTab('files');
					} else {
						openFileTab(filePath);
					}
				}}
			>
				{#if isDirectory}
					<svg
						class="w-3.5 h-3.5 shrink-0"
						viewBox="0 0 16 16"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M2.5 3A1.5 1.5 0 0 1 4 1.5h2.172a1.5 1.5 0 0 1 1.06.44l.768.767a1.5 1.5 0 0 0 1.06.439H12A1.5 1.5 0 0 1 13.5 4.5v8A1.5 1.5 0 0 1 12 14H4a1.5 1.5 0 0 1-1.5-1.5V3Z"
						/>
					</svg>
				{:else}
					<svg
						class="w-3.5 h-3.5 shrink-0"
						viewBox="0 0 16 16"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M9 1.5H4a1.5 1.5 0 0 0-1.5 1.5v10A1.5 1.5 0 0 0 4 14.5h8a1.5 1.5 0 0 0 1.5-1.5V6L9 1.5Z"
						/>
						<path d="M9 1.5V6h4.5" />
					</svg>
				{/if}
				{fileName}
			</button>
		{:else}
			<a href={href || '#'} target="_blank" rel="noopener noreferrer">
				{#if 'tokens' in item && item.tokens}
					<svelte:self items={item.tokens} />
				{:else}
					{'text' in item ? item.text : item.raw}
				{/if}
			</a>
		{/if}
	{:else if item.type === 'image'}
		<img
			src={'href' in item ? item.href : ''}
			alt={'text' in item ? item.text : ''}
			title={'title' in item ? item.title : undefined}
			loading="lazy"
		/>
	{:else if item.type === 'br'}
		<br />
	{:else if item.type === 'escape'}
		{'text' in item ? item.text : item.raw}
	{:else if item.type === 'html'}
		{@const wl = parseWikilink(item.raw)}
		{#if wl}
			<span
				class="text-blue-500 dark:text-blue-400 bg-blue-500/8 dark:bg-blue-400/10 rounded px-1 cursor-pointer hover:underline transition-colors"
				title={$t('markdown.linkTo', { target: wl.target })}>{wl.label}</span
			>
		{:else}
			{item.raw}
		{/if}
	{/if}
{/each}
