import React from 'react';
import { NavLink, Route, Routes, Navigate } from 'react-router-dom';
import Wizard from './pages/Wizard';
import SessionDetail from './pages/SessionDetail';
import Settings from './pages/Settings';

function EditorialHeader() {
  const link = ({ isActive }) => `text-[12px] tracking-widest uppercase font-medium ${isActive ? 'text-oxblood' : 'text-ink-2 hover:text-ink'}`;
  return (
    <header className="border-b border-rule bg-paper" data-testid="top-bar">
      <div className="max-w-7xl mx-auto px-10 h-16 flex items-center justify-between">
        <NavLink to="/" className="flex items-baseline gap-3">
          <span className="font-display text-xl text-ink">PHI Console</span>
          <span className="kicker text-ink-muted">Handled &nbsp;·&nbsp; verifiable &nbsp;·&nbsp; publishable</span>
        </NavLink>
        <nav className="flex items-center gap-8">
          <NavLink to="/" end className={link} data-testid="nav-new-run">New run</NavLink>
          <NavLink to="/settings" className={link} data-testid="nav-settings">Settings</NavLink>
        </nav>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-paper text-ink flex flex-col">
      <EditorialHeader />
      <main className="flex-1 min-h-0" data-testid="main-content">
        <Routes>
          <Route path="/" element={<Wizard />} />
          <Route path="/studies/:sid" element={<SessionDetail />} />
          <Route path="/settings" element={<Settings />} />
          {/* Legacy — redirect quietly */}
          <Route path="/studies" element={<Navigate to="/" replace />} />
          <Route path="/studies/new" element={<Navigate to="/" replace />} />
          <Route path="/sessions" element={<Navigate to="/" replace />} />
          <Route path="/sessions/new" element={<Navigate to="/" replace />} />
          <Route path="/sessions/:sid" element={<SessionDetail />} />
          <Route path="/benchmark" element={<Navigate to="/" replace />} />
          <Route path="/experimental/corpus" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
