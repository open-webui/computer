<script lang="ts">
	import { type Token, type Tokens } from 'marked';

	import CodeBlock from './CodeBlock.svelte';
	import MermaidBlock from './MermaidBlock.svelte';
	import InlineRenderer from './InlineRenderer.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import { t } from '$lib/i18n';

	interface Props {
		tokens: Token[];
	}

	let { tokens }: Props = $props();
</script>

{#each tokens as token}
	{#if token.type === 'mathBlock'}
		<KatexRenderer text={token.text} displayMode />
	{:else if token.type === 'heading'}
		{@const h = token as Tokens.Heading}
		<svelte:element this={`h${h.depth}`}>
			<InlineRenderer items={h.tokens} />
		</svelte:element>
	{:else if token.type === 'paragraph'}
		<p><InlineRenderer items={(token as Tokens.Paragraph).tokens} /></p>
	{:else if token.type === 'code'}
		{#if (token as Tokens.Code).lang === 'mermaid'}
			<MermaidBlock code={(token as Tokens.Code).text} />
		{:else if (token as Tokens.Code).lang === 'math'}
			<KatexRenderer text={(token as Tokens.Code).text} displayMode />
		{:else}
			<CodeBlock language={(token as Tokens.Code).lang ?? ''} code={(token as Tokens.Code).text} />
		{/if}
	{:else if token.type === 'blockquote'}
		<blockquote>
			<svelte:self tokens={(token as Tokens.Blockquote).tokens} />
		</blockquote>
	{:else if token.type === 'list'}
		{@const list = token as Tokens.List}
		<svelte:element this={list.ordered ? 'ol' : 'ul'} start={list.ordered ? list.start : undefined}>
			{#each list.items as item}
				<li>
					{#if item.task}
						<input type="checkbox" checked={item.checked} disabled />
					{/if}
					{#if item.tokens}
						<svelte:self tokens={item.tokens} />
					{/if}
				</li>
			{/each}
		</svelte:element>
	{:else if token.type === 'table'}
		{@const table = token as Tokens.Table}
		<div class="not-prose relative w-full group/table">
			<div
				class="overflow-x-auto max-w-full rounded-lg border border-gray-200/60 dark:border-white/8"
			>
				<table class="w-full text-[0.8125rem] text-left text-gray-600 dark:text-gray-300">
					<thead
						class="text-xs text-gray-500 dark:text-gray-400 uppercase bg-gray-50/80 dark:bg-white/[0.03]"
					>
						<tr>
							{#each table.header as cell, i}
								<th
									scope="col"
									class="px-3 py-1.5 font-medium border-b border-gray-200/60 dark:border-white/8 whitespace-nowrap"
									style={table.align[i] ? `text-align:${table.align[i]}` : ''}
								>
									<InlineRenderer items={cell.tokens} />
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each table.rows as row, rowIdx}
							<tr class="bg-white dark:bg-transparent">
								{#each row as cell, i}
									<td
										class="px-3 py-1.5 {rowIdx < table.rows.length - 1
											? 'border-b border-gray-100/60 dark:border-white/5'
											: ''}"
										style={table.align[i] ? `text-align:${table.align[i]}` : ''}
									>
										<InlineRenderer items={cell.tokens} />
									</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<!-- Utility buttons (show on hover) -->
			<div class="absolute top-1 right-1.5 invisible group-hover/table:visible flex gap-0.5">
				<button
					class="p-1 rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200/60 dark:hover:bg-white/8 transition-all duration-100"
					title={$t('common.copy')}
					onclick={() => {
						navigator.clipboard.writeText((token as Tokens.Table).raw?.trim() ?? '');
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.5"
						stroke="currentColor"
						class="size-3.5"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 0 1 1.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 0 0-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 0 1-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 0 0-3.375-3.375h-1.5a1.125 1.125 0 0 1-1.125-1.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H9.75"
						/>
					</svg>
				</button>
				<button
					class="p-1 rounded text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200/60 dark:hover:bg-white/8 transition-all duration-100"
					title={$t('common.downloadCsv')}
					onclick={() => {
						const tbl = token as Tokens.Table;
						const header = tbl.header.map((h) => `"${h.text.replace(/"/g, '""')}"`);
						const rows = tbl.rows.map((r) => r.map((c) => `"${c.text.replace(/"/g, '""')}"`));
						const csv = [header, ...rows].map((r) => r.join(',')).join('\n');
						const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=UTF-8' });
						const url = URL.createObjectURL(blob);
						const a = document.createElement('a');
						a.href = url;
						a.download = 'table.csv';
						a.click();
						URL.revokeObjectURL(url);
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.5"
						stroke="currentColor"
						class="size-3.5"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"
						/>
					</svg>
				</button>
			</div>
		</div>
	{:else if token.type === 'hr'}
		<hr class="border-gray-100/60 dark:border-gray-850/40" />
	{:else if token.type === 'text'}
		{#if 'tokens' in token && token.tokens}
			<InlineRenderer items={token.tokens} />
		{:else}
			{token.raw}
		{/if}
	{:else if token.type === 'html'}
		{(token as Tokens.HTML).text || token.raw}
	{:else if token.type === 'def'}
		<!-- Link reference definition, no visual output -->
	{:else if token.type === 'space'}
		<!-- skip -->
	{/if}
{/each}
