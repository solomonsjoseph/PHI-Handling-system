const defaultTheme = require('tailwindcss/defaultTheme');

module.exports = {
  content: ['./src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#090A0C',
        surface: '#111317',
        'surface-2': '#1C1F26',
        border: '#272A31',
        'text-primary': '#F9FAFB',
        'text-secondary': '#9CA3AF',
        'text-muted': '#6B7280',
        phi: '#B45309',
        'phi-bg': 'rgba(180, 83, 9, 0.15)',
        'phi-border': '#92400E',
        accept: '#047857',
        reject: '#B91C1C',
        info: '#1D4ED8',
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', ...defaultTheme.fontFamily.sans],
        display: ['"Bricolage Grotesque"', ...defaultTheme.fontFamily.sans],
        mono: ['"JetBrains Mono"', ...defaultTheme.fontFamily.mono],
      },
      borderRadius: {
        DEFAULT: '0',
        none: '0',
      },
    },
  },
  plugins: [],
};
