import React from 'react';

// deterministic monospace progress indicator: [==========>         ] 50%
export function MonoProgress({ percent = 0, width = 40 }) {
  const filled = Math.max(0, Math.min(width, Math.round((percent / 100) * width)));
  const bar = '='.repeat(Math.max(0, filled - 1)) + (percent > 0 && percent < 100 ? '>' : '=').padEnd(1);
  const rest = ' '.repeat(Math.max(0, width - filled));
  return (
    <span className="progress-mono text-xs" data-testid="mono-progress">
      [{bar}{rest}] {String(Math.round(percent)).padStart(3, ' ')}%
    </span>
  );
}

export function Panel({ title, cite, right, children, testId }) {
  return (
    <section className="border-b border-border" data-testid={testId}>
      <div className="h-10 px-4 border-b border-border flex items-center justify-between bg-surface">
        <div className="flex items-center gap-3">
          <span className="font-mono text-[10px] uppercase tracking-widest text-text-muted">{title}</span>
          {cite && <span className="font-mono text-[10px] text-phi">{cite}</span>}
        </div>
        <div>{right}</div>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Btn({ children, variant = 'default', testId, disabled, onClick, type = 'button' }) {
  const base = 'h-9 px-4 text-xs font-mono uppercase tracking-widest border transition-colors duration-100 disabled:opacity-30 disabled:cursor-not-allowed';
  const styles = {
    default: 'bg-surface border-border text-text-primary hover:bg-surface-2 hover:border-text-secondary',
    accept: 'bg-surface border-accept text-accept hover:bg-accept hover:text-white',
    reject: 'bg-surface border-reject text-reject hover:bg-reject hover:text-white',
    danger: 'bg-reject border-reject text-white hover:bg-red-800',
    primary: 'bg-text-primary border-text-primary text-bg hover:bg-text-secondary',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className={`${base} ${styles[variant] || styles.default}`}
    >
      {children}
    </button>
  );
}

export function Tag({ children, color = 'default', testId }) {
  const colors = {
    default: 'border-border text-text-secondary bg-surface',
    phi: 'border-phi-border text-phi bg-phi-bg',
    accept: 'border-accept text-accept',
    reject: 'border-reject text-reject',
    info: 'border-info text-info',
  };
  return (
    <span data-testid={testId} className={`inline-flex items-center px-2 h-5 border text-[10px] font-mono uppercase tracking-widest ${colors[color]}`}>
      {children}
    </span>
  );
}
