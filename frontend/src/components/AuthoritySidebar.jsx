import React from 'react';

const CATS = [
  ['A', 'Names', '164.514(b)(2)(i)(A)'],
  ['B', 'Geographic subdivisions', '(B)'],
  ['C', 'Dates; age > 89', '(C)'],
  ['D', 'Telephone numbers', '(D)'],
  ['E', 'Fax numbers', '(E)'],
  ['F', 'Email addresses', '(F)'],
  ['G', 'Social Security numbers', '(G)'],
  ['H', 'Medical record numbers', '(H)'],
  ['I', 'Health plan beneficiary #', '(I)'],
  ['J', 'Account numbers', '(J)'],
  ['K', 'Certificate/license #', '(K)'],
  ['L', 'Vehicle identifiers', '(L)'],
  ['M', 'Device identifiers', '(M)'],
  ['N', 'Web URLs', '(N)'],
  ['O', 'IP addresses', '(O)'],
  ['P', 'Biometric identifiers', '(P)'],
  ['Q', 'Full-face photographs', '(Q)'],
  ['R', 'Any other unique code', '(R)'],
];

export default function AuthoritySidebar() {
  return (
    <aside
      className="w-80 border-l border-border bg-surface hidden lg:flex flex-col"
      data-testid="authority-sidebar"
    >
      <div className="px-4 py-3 border-b border-border">
        <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted">Authority Matrix</div>
        <div className="font-display text-base tracking-tight">HIPAA 45 CFR 164.514</div>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-xs font-mono">
          <tbody>
            {CATS.map(([code, label, cite]) => (
              <tr key={code} className="border-b border-border" data-testid={`authority-row-${code}`}>
                <td className="px-3 py-2 text-phi font-semibold w-8 border-r border-border">{code}</td>
                <td className="px-3 py-2 text-text-primary">
                  <div>{label}</div>
                  <div className="text-[10px] text-text-muted mt-0.5">{cite}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="px-4 py-3 border-t border-border">
          <div className="font-mono text-[10px] uppercase tracking-widest text-text-muted mb-1">Also enforced</div>
          <ul className="font-mono text-[11px] text-text-secondary space-y-1">
            <li>164.514(b)(2)(ii) actual knowledge</li>
            <li>164.514(c) re-identification codes</li>
            <li>164.514(e) limited data set</li>
            <li>164.514(f) fundraising context</li>
            <li>Sweeney 2002 k-anonymity</li>
          </ul>
        </div>
      </div>
    </aside>
  );
}
