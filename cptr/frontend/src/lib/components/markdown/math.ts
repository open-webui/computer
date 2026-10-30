import { Marked, type TokenizerExtension } from 'marked';

// Tokenize before Markdown can consume TeX backslashes, underscores or asterisks.
// Code spans and ordinary code fences continue to use Marked's code tokens.
function escaped(source: string, index: number): boolean {
	let slashes = 0;
	while (index > 0 && source[--index] === '\\') slashes++;
	return slashes % 2 === 1;
}

function mathToken(source: string, block: boolean) {
	const pairs = block
		? [
				['$$', '$$'],
				['\\[', '\\]']
			]
		: [
				['$$', '$$'],
				['\\[', '\\]'],
				['\\(', '\\)'],
				['$', '$']
			];
	for (const [open, close] of pairs) {
		if (!source.startsWith(open)) continue;
		const singleDollar = open === '$';
		if (singleDollar && (source.startsWith('$$') || /\s/.test(source[1] ?? ''))) continue;
		let end = open.length;
		while ((end = source.indexOf(close, end)) !== -1) {
			if (escaped(source, end)) {
				end += close.length;
				continue;
			}
			if (singleDollar && (/\s/.test(source[end - 1]) || /[\d$]/.test(source[end + 1] ?? ''))) {
				return;
			}
			const text = source.slice(open.length, end);
			if (!text.trim() || (singleDollar && /[\n`]/.test(text))) return;
			const raw = source.slice(0, end + close.length);
			if (block && !/^[ \t]*(?:\n|$)/.test(source.slice(raw.length))) return;
			return {
				type: block ? 'mathBlock' : 'mathInline',
				raw,
				text,
				displayMode: open === '$$' || open === '\\['
			};
		}
		return;
	}
}

const blockMath: TokenizerExtension = {
	name: 'mathBlock',
	level: 'block',
	start(source) {
		return source.match(/(?:^|\n)(?=\$\$|\\\[)/)?.index;
	},
	tokenizer(source) {
		return mathToken(source, true);
	}
};

const inlineMath: TokenizerExtension = {
	name: 'mathInline',
	level: 'inline',
	start(source) {
		return source.match(/\$|\\[([]/)?.index;
	},
	tokenizer(source) {
		return mathToken(source, false);
	}
};

export const mathMarkdown = new Marked({ extensions: [blockMath, inlineMath] });
