/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ink: { 900: '#0b0d0f', 800: '#12151a', 700: '#1a1f26', 600: '#252b34' },
        edge: '#2a313b',
        pos: '#4ade80',
        neg: '#f87171',
        warn: '#fbbf24',
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
