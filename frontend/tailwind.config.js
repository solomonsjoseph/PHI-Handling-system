const defaultTheme = require('tailwindcss/defaultTheme');

module.exports = {
  content: ['./src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Clinical/academic palette — paper first, oxblood accent
        paper: '#F7F5F0',            // warm off-white background
        'paper-2': '#EFEBE3',         // panel background
        'paper-3': '#E4DFD5',         // deeper panel / divider
        ink: '#12141A',               // near-black body text
        'ink-2': '#2A2D35',           // secondary text
        'ink-muted': '#6B6E76',       // captions
        rule: '#D6D0C4',              // hairline dividers
        oxblood: '#8C2135',           // single accent — links, primary CTA
        'oxblood-2': '#6E1928',       // hover
        signal: '#B37A00',            // amber for warnings/pending
        clean: '#2F6E4E',             // muted forest green for guard-clean
        blocked: '#8C2135',           // oxblood also = blocked
      },
      fontFamily: {
        // Editorial serif for headings, humanist sans for body, mono for data only
        display: ['"Fraunces"', '"Iowan Old Style"', 'Georgia', ...defaultTheme.fontFamily.serif],
        sans: ['"Inter"', '"IBM Plex Sans"', ...defaultTheme.fontFamily.sans],
        mono: ['"JetBrains Mono"', ...defaultTheme.fontFamily.mono],
      },
      fontSize: {
        // Precise editorial hierarchy
        'display-xl': ['4.5rem', { lineHeight: '1.02', letterSpacing: '-0.03em' }],
        'display-lg': ['3.25rem', { lineHeight: '1.05', letterSpacing: '-0.025em' }],
        'display-md': ['2.25rem', { lineHeight: '1.1', letterSpacing: '-0.02em' }],
        'display-sm': ['1.5rem', { lineHeight: '1.2', letterSpacing: '-0.015em' }],
        'label': ['0.6875rem', { lineHeight: '1', letterSpacing: '0.14em' }],
        'body': ['0.9375rem', { lineHeight: '1.55' }],
      },
      borderRadius: {
        DEFAULT: '0',
        none: '0',
      },
      spacing: {
        18: '4.5rem',
        22: '5.5rem',
        30: '7.5rem',
      },
    },
  },
  plugins: [],
};
