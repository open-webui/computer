<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { t } from '$lib/i18n';
	import { expandToolDetails } from '$lib/stores';

	interface Props {
		item: any;
		pairedOutput?: any;
		done: boolean;
		chatId: string | null;
		messageId: string;
		toolLabel: (name: string, args: any) => string;
		onapprove: (messageId: string, callId: string, approved: boolean) => void;
	}

	let { item, pairedOutput, done, chatId, messageId, toolLabel, onapprove }: Props = $props();

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

	let expanded = $state($expandToolDetails || item.name === 'ask_user');
	$effect(() => {
		expanded = $expandToolDetails || item.name === 'ask_user';
	});

	const args = $derived(item.arguments || {});
	const toolName = $derived(item.name);
	const callId = $derived(item.call_id || toolName);
	const isExecuting = $derived(
		item.status === 'running' || item.status === 'in_progress' || (!item.status && !done)
	);
	const isError = $derived(
		item.status === 'failed' || isToolResultError(pairedOutput?.output ?? '')
	);
	const isDone = $derived(item.status === 'completed' || isError);
	const isRejected = $derived(item.status === 'rejected');
	const isPending = $derived(item.status === 'pending');
	const isAskUser = $derived(toolName === 'ask_user');
	const askUserQuestions = $derived(Array.isArray(args.questions) ? args.questions : []);
	const askUserAnswers = $derived.by(() => {
		try {
			return JSON.parse(pairedOutput?.output || '{}').answers || {};
		} catch {
			return {};
		}
	});
	const imageToolOutput = $derived.by(() => {
		if (toolName !== 'image_generate' || !pairedOutput?.output) {
			return null;
		}
		try {
			const parsed = JSON.parse(pairedOutput.output);
			const images = Array.isArray(parsed.images) ? parsed.images : [];
			return images.filter((image: any) => image && (image.path || image.url || image.id));
		} catch {
			return null;
		}
	});
	const formattedOutput = $derived.by(() => {
		if (!pairedOutput?.output) return '';
		try {
			return JSON.stringify(JSON.parse(pairedOutput.output), null, 2);
		} catch {
			return pairedOutput.output;
		}
	});
	function toggleExpanded() {
		expanded = !expanded;
	}

	function handleRowKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			toggleExpanded();
		}
	}
</script>

<div class="w-full min-w-0 flex flex-col">
	<div
		role="button"
		tabindex="0"
		class="w-full min-w-0 text-left text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition cursor-pointer"
		aria-expanded={expanded}
		aria-controls={callId}
		onclick={toggleExpanded}
		onkeydown={handleRowKeydown}
	>
		<div class="flex items-center gap-1.5 text-sm min-w-0 {isExecuting ? 'shimmer' : ''}">
			{#if isExecuting}
				<div class="flex justify-center text-center">
					<svg
						aria-hidden="true"
						class="size-4"
						viewBox="0 0 24 24"
						fill="currentColor"
						xmlns="http://www.w3.org/2000/svg"
					>
						<style>
							.spinner_inner {
								transform-origin: center;
								animation: spinner_inner_a 0.75s infinite linear;
							}
							@keyframes spinner_inner_a {
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
							class="spinner_inner"
						/>
					</svg>
				</div>
			{:else if isAskUser}
				<div class="text-gray-400 dark:text-gray-500">
					<svg
						aria-hidden="true"
						class="size-3.5"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.75"
					>
						<path stroke-linecap="round" stroke-linejoin="round" d="m5 12 3 3 5-7" />
						<path stroke-linecap="round" stroke-linejoin="round" d="M13 7h6M13 12h6M13 17h6" />
					</svg>
				</div>
			{:else if isError}
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
			{:else if isDone}
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
			{:else if isRejected}
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
				<span class="font-normal">{toolLabel(toolName, args)}</span>
			</div>

			{#if isPending && chatId}
				<span class="flex gap-1 shrink-0">
					<button
						class="text-[0.6875rem] px-2.5 py-0.5 rounded-md
						text-gray-600 dark:text-gray-300
						bg-gray-100 dark:bg-white/8
						hover:bg-gray-200 dark:hover:bg-white/12
						transition-colors duration-100"
						onclick={(e) => {
							e.stopPropagation();
							onapprove(messageId, item.call_id, true);
						}}>{$t('chat.allow')}</button
					>
					<button
						class="text-[0.6875rem] px-2 py-0.5 rounded-md
						text-gray-400 dark:text-gray-500
						hover:text-gray-600 dark:hover:text-gray-300
						transition-colors duration-100"
						onclick={(e) => {
							e.stopPropagation();
							onapprove(messageId, item.call_id, false);
						}}>{$t('chat.deny')}</button
					>
				</span>
			{:else}
				<div class="flex shrink-0 items-center gap-1 self-center">
					<svg
						xmlns="http://www.w3.org/2000/svg"
						fill="none"
						viewBox="0 0 24 24"
						stroke-width="3.5"
						stroke="currentColor"
						class="size-3 transition-transform duration-200 text-gray-400 dark:text-gray-500 {expanded
							? 'rotate-180'
							: ''}"
					>
						<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
					</svg>
				</div>
			{/if}
		</div>
	</div>

	{#if expanded}
		<div id={callId} transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
			<div
				class={isAskUser
					? 'ml-5 mt-1.5 space-y-1.5 text-xs leading-relaxed'
					: 'border border-gray-50 dark:border-gray-850/30 rounded-2xl my-1.5 p-3 space-y-3 overflow-hidden'}
			>
				{#if isAskUser}
					{#each askUserQuestions as question}
						{@const answer = askUserAnswers[question.id]?.answers?.join(', ')}
						<div>
							<div class="text-gray-600 dark:text-gray-300">{question.question}</div>
							<div class="text-gray-400 dark:text-gray-500">
								{answer || $t('chat.noAnswerRecorded')}{answer === question.options?.[0]?.label
									? ` (${$t('chat.recommended')})`
									: ''}
							</div>
						</div>
					{/each}
				{:else}
					{#if Object.keys(args).length > 0}
						<div>
							<div
								class="text-[0.625rem] uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1.5 px-1"
							>
								{$t('chat.toolInput')}
							</div>
							{#if toolName === 'edit_file' && args.target}
								<div
									class="text-[0.6875rem] font-mono rounded-lg overflow-hidden border border-gray-200/60 dark:border-white/6"
								>
									<div
										class="bg-red-50/80 dark:bg-red-950/20 text-red-700 dark:text-red-400 px-2.5 py-1 whitespace-pre-wrap break-all leading-relaxed"
									>
										<span class="select-none text-red-400 dark:text-red-600 mr-1">-</span>{args
											.target.length > 500
											? args.target.slice(0, 500) + '...'
											: args.target}
									</div>
									<div
										class="bg-green-50/80 dark:bg-green-950/20 text-green-700 dark:text-green-400 px-2.5 py-1 whitespace-pre-wrap break-all leading-relaxed"
									>
										<span class="select-none text-green-400 dark:text-green-600 mr-1">+</span>{(
											args.replacement || ''
										).length > 500
											? args.replacement.slice(0, 500) + '...'
											: args.replacement || ''}
									</div>
								</div>
								{#if args.start_line}
									<div class="text-[0.625rem] text-gray-400 dark:text-gray-600 mt-1 px-1">
										{$t('chat.toolLines', {
											start: args.start_line,
											end: args.end_line || 'end'
										})}
									</div>
								{/if}
							{:else if toolName === 'run_command'}
								<code
									class="block text-xs font-mono text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 rounded-lg px-2.5 py-1.5 overflow-x-auto break-all whitespace-pre-wrap"
									>{args.command}</code
								>
							{:else}
								<div class="px-1 space-y-0.5">
									{#each Object.entries(args) as [key, value]}
										<div class="flex gap-2 text-xs py-0.5">
											<span class="text-gray-600 dark:text-gray-400 shrink-0">{key}</span>
											<span class="text-gray-800 dark:text-gray-200 break-all whitespace-pre-wrap">
												{typeof value === 'object' ? JSON.stringify(value, null, 2) : value}
											</span>
										</div>
									{/each}
								</div>
							{/if}
						</div>
					{/if}

					{#if pairedOutput?.output}
						<div>
							<div
								class="text-[0.625rem] uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1.5 px-1"
							>
								{$t('chat.toolOutput')}
							</div>
							<div class="w-full min-w-0 overflow-hidden">
								{#if imageToolOutput?.length}
									<div class="px-1 space-y-1">
										{#each imageToolOutput as image}
											<div
												class="text-xs text-gray-600 dark:text-gray-300 break-all leading-relaxed"
											>
												{image.path || image.url || image.id}
											</div>
										{/each}
									</div>
								{:else}
									<pre
										class="text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words font-mono max-h-64 overflow-auto leading-relaxed">{formattedOutput.length >
										10000
											? formattedOutput.slice(0, 10000)
											: formattedOutput}</pre>
								{/if}
								{#if !imageToolOutput?.length && formattedOutput.length > 10000}
									<div class="text-[0.625rem] text-gray-400 dark:text-gray-600 mt-1 px-1">
										{$t('chat.totalChars', {
											count: formattedOutput.length.toLocaleString()
										})}
									</div>
								{/if}
							</div>
						</div>
					{/if}
				{/if}
			</div>
		</div>
	{/if}
</div>
