import { setTextScale } from '$lib/utils/text-scale';

export type Theme = 'dark' | 'light' | 'system';

export type ThemeColors = {
	background?: string;
	foreground?: string;
};

export type ThemeConfig = {
	light?: ThemeColors;
	dark?: ThemeColors;
	uiFont?: string;
};

export type AppearancePreferences = {
	theme?: Theme;
	themeConfig?: ThemeConfig | null;
	textScale?: number | null;
	borderContrast?: number | null;
	highContrastBorders?: boolean;
};

type ResolvedTheme = 'dark' | 'light';

const DEFAULT_UI_FONT =
	"'Inter', -apple-system, BlinkMacSystemFont, ui-sans-serif, system-ui, sans-serif";
export const DEFAULT_BORDER_CONTRAST = 1.5;
export const DEFAULT_DIVIDER_CONTRAST = 0.875;
export const MAX_BORDER_CONTRAST = 16;

export function normalizeBorderContrast(value: unknown): number | null {
	if (value === null || value === undefined || value === '') return null;
	const contrast = Number(value);
	if (!Number.isFinite(contrast)) return null;
	return Math.max(
		DEFAULT_BORDER_CONTRAST,
		Math.min(MAX_BORDER_CONTRAST, Number(contrast.toFixed(1)))
	);
}

export function resolveThemeMode(theme: Theme): ResolvedTheme {
	if (theme === 'system' && typeof window !== 'undefined') {
		return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
	}
	return theme === 'light' ? 'light' : 'dark';
}

export function normalizeHexColor(value: unknown): string | undefined {
	if (typeof value !== 'string') return undefined;
	const color = value.trim();
	const short = /^#([0-9a-f]{3})$/i.exec(color);
	if (short) {
		return `#${short[1]
			.split('')
			.map((char) => char + char)
			.join('')}`.toLowerCase();
	}
	if (/^#[0-9a-f]{6}$/i.test(color)) return color.toLowerCase();
	return undefined;
}

function sanitizeThemeColors(value: unknown): ThemeColors | null {
	if (!value || typeof value !== 'object') return null;
	const raw = value as Record<string, unknown>;
	const next: ThemeColors = {};
	const background = normalizeHexColor(raw.background);
	const foreground = normalizeHexColor(raw.foreground);
	if (background) next.background = background;
	if (foreground) next.foreground = foreground;
	return Object.keys(next).length ? next : null;
}

export function sanitizeThemeConfig(value: unknown): ThemeConfig | null {
	if (!value || typeof value !== 'object') return null;
	const raw = value as Record<string, unknown>;
	const next: ThemeConfig = {};
	const light = sanitizeThemeColors(raw.light);
	const dark = sanitizeThemeColors(raw.dark);
	const legacy = sanitizeThemeColors(raw);
	if (light) next.light = light;
	if (dark) next.dark = dark;
	if (legacy && !light && !dark) {
		next.light = legacy;
		next.dark = legacy;
	}
	if (typeof raw.uiFont === 'string' && raw.uiFont.trim()) {
		next.uiFont = raw.uiFont.trim().slice(0, 240);
	}
	return Object.keys(next).length ? next : null;
}

export function defaultThemeConfig(theme: Theme): Required<ThemeColors> & { uiFont: string } {
	const resolved = resolveThemeMode(theme);
	return {
		background: resolved === 'dark' ? '#121212' : '#ffffff',
		foreground: resolved === 'dark' ? '#d4d4d4' : '#525252',
		uiFont: DEFAULT_UI_FONT
	};
}

export function resolveThemeConfig(
	theme: Theme,
	config: ThemeConfig | null
): Required<ThemeColors> & { uiFont: string } {
	const resolved = resolveThemeMode(theme);
	return {
		...defaultThemeConfig(theme),
		...(config?.[resolved] ?? {}),
		uiFont: config?.uiFont ?? DEFAULT_UI_FONT
	};
}

function setVar(name: string, value: string) {
	document.documentElement.style.setProperty(name, value);
}

export function applyAppearance(
	theme: Theme,
	config: ThemeConfig | null,
	textScale: number | null,
	borderContrast: number | null = null
) {
	if (typeof document === 'undefined') return;

	const resolved = resolveThemeMode(theme);
	const merged = resolveThemeConfig(theme, config);
	const borderMix = normalizeBorderContrast(borderContrast) ?? DEFAULT_BORDER_CONTRAST;
	const dividerMix =
		borderMix === DEFAULT_BORDER_CONTRAST
			? DEFAULT_DIVIDER_CONTRAST
			: Number(((borderMix * 2) / 3).toFixed(3));

	document.documentElement.classList.toggle('dark', resolved === 'dark');
	document.documentElement.style.colorScheme = resolved;

	setVar('--app-bg', merged.background);
	setVar('--app-fg', merged.foreground);
	setVar('--app-border', `color-mix(in oklab, var(--app-fg) ${borderMix}%, transparent)`);
	setVar('--app-divider', `color-mix(in oklab, var(--app-fg) ${dividerMix}%, transparent)`);
	setVar('--app-ui-font', merged.uiFont);
	setVar('--font-sans', merged.uiFont);

	setTextScale(textScale ?? 1);

	const meta = document.querySelector('meta[name="theme-color"]');
	if (meta) meta.setAttribute('content', merged.background);
}
