import { readonly, ref } from 'vue';

export type ThemeMode = 'dark' | 'light';

const STORAGE_KEY = 'agentforge.theme';

function readInitialTheme(): ThemeMode {
  try {
    const saved = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') {
      return saved;
    }
  } catch {
    // 无痕或测试环境不可读写 localStorage 时安全退回默认深色
  }
  return 'dark';
}

const currentTheme = ref<ThemeMode>(readInitialTheme());

export function applyThemeToDom(mode: ThemeMode): void {
  if (typeof document !== 'undefined' && document.documentElement) {
    document.documentElement.setAttribute('data-theme', mode);
    document.documentElement.classList.toggle('dark', mode === 'dark');
  }
}

export function setTheme(mode: ThemeMode): void {
  currentTheme.value = mode;
  applyThemeToDom(mode);
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore storage quota errors
  }
}

export function toggleTheme(): ThemeMode {
  const next: ThemeMode = currentTheme.value === 'dark' ? 'light' : 'dark';
  setTheme(next);
  return next;
}

export function useTheme() {
  applyThemeToDom(currentTheme.value);
  return {
    theme: readonly(currentTheme),
    setTheme,
    toggleTheme,
  };
}
