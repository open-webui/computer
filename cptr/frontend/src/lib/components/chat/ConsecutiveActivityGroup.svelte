<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { t } from '$lib/i18n';
	import { expandToolDetails } from '$lib/stores';
	import ReasoningCollapsible from './ReasoningCollapsible.svelte';
	import ToolCallCollapsible from './ToolCallCollapsible.svelte';

	interface Props {
		entries: any[];
		calls: any[];
		reasoning: any[];
		outputs: Map<string, any>;
		done: boolean;
		chatId: string | null;
		messageId: string;
		groupIdx: number;
		toolLabel: (name: string, args: any) => string;
		onapprove: (messageId: string, callId: string, approved: boolean) => void;
	}

	let {
		entries,
		calls,
		reasoning,
		outputs,
		done,
		chatId,
		messageId,
		groupIdx,
		toolLabel,
		onapprove
	}: Props = $props();

	function isToolResultError(value: unknown): boolean {
		if (typeof value === 'string') {
			const text = value.trim().toLowerCase();
			if (
				text.startsWith('error:') ||
				text.startsWith('exception:') ||
				text.startsWith('traceback') ||
				text.startsWith('http error!')
			) {
				return true;
			}
		}

		let parsed = value;
		while (typeof parsed === 'string') {
			try {
				parsed = JSON.parse(parsed);
			} catch {
				break;
			}
		}
		if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return false;

		const result = parsed as Record<string, unknown>;
		const error = result.error;
		if (
			(typeof error === 'string' && error.trim().length > 0) ||
			(typeof error === 'object' && error !== null)
		) {
			return true;
		}

		const status = typeof result.status === 'string' ? result.status.trim().toLowerCase() : '';
		if (status === 'error' || status === 'failed') return true;

		const message = result.message;
		return (
			(result.success === false || result.ok === false) &&
			((typeof message === 'string' && message.trim().length > 0) ||
				(typeof message === 'object' && message !== null))
		);
	}

	let expanded = $state($expandToolDetails);
	$effect(() => {
		expanded = $expandToolDetails;
	});

	const hasReasoningPending = $derived(
		reasoning.some((ri: any) => ri.status === 'in_progress' || ri.status === 'running')
	);
	const hasPending = $derived(
		(!done && calls.some((c: any) => !['completed', 'failed', 'rejected'].includes(c.status))) ||
			hasReasoningPending
	);
	const allDone = $derived(
		(calls.length === 0 || calls.every((c: any) => ['completed', 'failed'].includes(c.status))) &&
			!hasReasoningPending
	);
	const hasRejected = $derived(calls.some((c: any) => c.status === 'rejected'));
	const hasError = $derived(
		calls.some(
			(c: any) => c.status === 'failed' || isToolResultError(outputs.get(c.call_id)?.output ?? '')
		)
	);
	const pendingCalls = $derived(calls.filter((c: any) => c.status === 'pending'));
	const hasPendingApproval = $derived(pendingCalls.length > 0);
	const isThoughtOnly = $derived(calls.length === 0);
	const summaryText = $derived.by(() => groupSummaryText(calls));

	function toggleExpanded() {
		expanded = !expanded;
	}

	function groupSummaryText(groupCalls: any[]): string {
		if (groupCalls.length <= 3) {
			return groupCalls
				.map((call: any) => toolLabel(call.name || 'tool', call.arguments || {}))
				.join(', ');
		}

		const nameCounts: Record<string, number> = {};
		for (const call of groupCalls) {
			const name = call.name || 'tool';
			nameCounts[name] = (nameCounts[name] || 0) + 1;
		}

		return Object.entries(nameCounts)
			.map(([name, count]) => (count > 1 ? `${count} ${name}` : name))
			.join(', ');
	}
</script>

<div class="w-full min-w-0 flex flex-col my-0.5">
	<button
		class="w-full min-w-0 text-left text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition cursor-pointer"
		aria-label={$t('chat.toggleToolCalls')}
		aria-expanded={expanded}
		onclick={toggleExpanded}
	>
		<div class="flex items-center gap-1.5 text-sm min-w-0 {hasPending ? 'shimmer' : ''}">
			{#if isThoughtOnly}
				<div class="text-gray-400 dark:text-gray-500">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.75"
						stroke="currentColor"
						class="size-3.5"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0 3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z"
						/>
					</svg>
				</div>
			{:else if hasPending}
				<div class="flex justify-center text-center">
					<svg
						aria-hidden="true"
						class="size-4"
						viewBox="0 0 24 24"
						fill="currentColor"
						xmlns="http://www.w3.org/2000/svg"
					>
						<style>
							.spinner_tc {
								transform-origin: center;
								animation: spinner_tc_a 0.75s infinite linear;
							}
							@keyframes spinner_tc_a {
								100% {
									transform: rotate(360deg);
								}
							}
						</style>
						<path
							d="M12,1A11,11,0,1,0,23,12,11,11,0,0,0,12,1Zm0,19a8,8,0,1,1,8-8A8,8,0,0,1,12,20Z"
							opacity=".25"
						/>
						<path
							d="M10.14,1.16a11,11,0,0,0-9,8.92A1.59,1.59,0,0,0,2.46,12,1.52,1.52,0,0,0,4.11,10.7a8,8,0,0,1,6.66-6.61A1.42,1.42,0,0,0,12,2.69h0A1.57,1.57,0,0,0,10.14,1.16Z"
							class="spinner_tc"
						/>
					</svg>
				</div>
			{:else if hasRejected}
				<div class="text-red-400 dark:text-red-500">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="2.5"
						stroke="currentColor"
						class="size-4"
					>
						<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</div>
			{:else if hasError}
				<div class="text-red-500 dark:text-red-400">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="2"
						stroke="currentColor"
						class="size-4"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M12 9v4m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
						/>
					</svg>
				</div>
			{:else if allDone}
				<div class="text-emerald-500 dark:text-emerald-400">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="2"
						stroke="currentColor"
						class="size-4"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
						/>
					</svg>
				</div>
			{:else}
				<div class="text-gray-400 dark:text-gray-500">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="1.75"
						stroke="currentColor"
						class="size-3.5"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							d="M21.75 6.75a4.5 4.5 0 0 1-4.884 4.484c-1.076-.091-2.264.071-2.95.904l-7.152 8.684a2.548 2.548 0 1 1-3.586-3.586l8.684-7.152c.833-.686.995-1.874.904-2.95a4.5 4.5 0 0 1 6.336-4.486l-3.276 3.276a3.004 3.004 0 0 0 2.25 2.25l3.276-3.276c.256.565.398 1.192.398 1.852Z"
						/>
					</svg>
				</div>
			{/if}

			<div class="flex-1 min-w-0 line-clamp-1">
				<span class="text-gray-600 dark:text-gray-300">
					{isThoughtOnly
						? hasPending
							? $t('chat.thinking')
							: $t('chat.edit.thought')
						: hasPending
							? $t('chat.exploring')
							: $t('chat.explored')}
				</span>
				{#if !isThoughtOnly && summaryText}
					<span class="text-gray-400 dark:text-gray-500 ml-1">{summaryText}</span>
				{/if}
			</div>

			<div class="flex shrink-0 self-center text-gray-400 dark:text-gray-500">
				<svg
					xmlns="http://www.w3.org/2000/svg"
					fill="none"
					viewBox="0 0 24 24"
					stroke-width="3.5"
					stroke="currentColor"
					class="size-3 transition-transform duration-200 {expanded ? 'rotate-180' : ''}"
				>
					<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
				</svg>
			</div>
		</div>
	</button>

	{#if expanded}
		<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
			<div class="mb-0.5 space-y-0.5 mt-1">
				{#each entries as item, entryIdx}
					{#if item.type === 'reasoning'}
						<ReasoningCollapsible {item} fallbackId={`reasoning-${groupIdx}-${entryIdx}`} />
					{:else}
						<ToolCallCollapsible
							{item}
							pairedOutput={outputs.get(item.call_id)}
							{done}
							{chatId}
							{messageId}
							{toolLabel}
							{onapprove}
						/>
					{/if}
				{/each}
			</div>
		</div>
	{/if}

	{#if !expanded && hasPendingApproval && chatId}
		<div class="mt-1 space-y-0.5">
			{#each pendingCalls as item}
				<div class="flex items-center gap-2 py-1 px-1">
					<span class="text-xs text-gray-500 dark:text-gray-400 flex-1 min-w-0 line-clamp-1">
						{toolLabel(item.name, item.arguments || {})}
					</span>
					<span class="flex gap-1 shrink-0">
						<button
							class="text-[0.6875rem] px-2.5 py-0.5 rounded-md
							text-gray-600 dark:text-gray-300
							bg-gray-100 dark:bg-white/8
							hover:bg-gray-200 dark:hover:bg-white/12
							transition-colors duration-100"
							onclick={() => onapprove(messageId, item.call_id, true)}>{$t('chat.allow')}</button
						>
						<button
							class="text-[0.6875rem] px-2 py-0.5 rounded-md
							text-gray-400 dark:text-gray-500
							hover:text-gray-600 dark:hover:text-gray-300
							transition-colors duration-100"
							onclick={() => onapprove(messageId, item.call_id, false)}>{$t('chat.deny')}</button
						>
					</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
