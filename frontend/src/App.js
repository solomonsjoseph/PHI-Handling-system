import React from 'react';
import { NavLink, Route, Routes, Navigate } from 'react-router-dom';
import Sessions from './pages/Sessions';
import SessionDetail from './pages/SessionDetail';
import NewSession from './pages/NewSession';
import Corpus from './pages/Corpus';
import Benchmark from './pages/Benchmark';
import AuthoritySidebar from './components/AuthoritySidebar';

function TopBar() {
  const link = ({ isActive }) => `px-4 h-12 flex items-center border-r border-border text-xs font-mono uppercase tracking-widest ${isActive ? 'bg-surface text-text-primary' : 'text-text-secondary hover:text-text-primary hover:bg-surface-2'}`;
  return (
    <header className="h-12 border-b border-border flex items-stretch bg-bg" data-testid="top-bar">
      <div className="px-4 flex items-center border-r border-border">
        <span className="font-mono text-xs tracking-widest text-text-primary">PHI/CONSOLE</span>
        <span className="ml-2 font-mono text-[10px] text-text-muted">v2.0.0</span>
      </div>
      <nav className="flex items-stretch">
        <NavLink to="/sessions" className={link} data-testid="nav-sessions">Sessions</NavLink>
        <NavLink to="/sessions/new" className={link} data-testid="nav-new-session">New</NavLink>
        <NavLink to="/corpus" className={link} data-testid="nav-corpus">Corpus</NavLink>
        <NavLink to="/benchmark" className={link} data-testid="nav-benchmark">Benchmark</NavLink>
      </nav>
      <div className="ml-auto px-4 flex items-center font-mono text-[10px] text-text-muted">
        45 CFR 164.514 &middot; HIPAA Safe Harbor &middot; DPDPA 2023
      </div>
    </header>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-bg text-text-primary flex flex-col">
      <TopBar />
      <div className="flex flex-1 min-h-0">
        <main className="flex-1 min-w-0 overflow-auto" data-testid="main-content">
          <Routes>
            <Route path="/" element={<Navigate to="/sessions" replace />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/sessions/new" element={<NewSession />} />
            <Route path="/sessions/:sid" element={<SessionDetail />} />
            <Route path="/corpus" element={<Corpus />} />
            <Route path="/benchmark" element={<Benchmark />} />
          </Routes>
        </main>
        <AuthoritySidebar />
      </div>
    </div>
  );
}
