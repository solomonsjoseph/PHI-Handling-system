import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { Toaster } from 'sonner';
import App from './App';
import './index.css';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
      <Toaster theme="dark" position="bottom-right" toastOptions={{ style: { background: '#111317', border: '1px solid #272A31', borderRadius: 0, color: '#F9FAFB', fontFamily: 'JetBrains Mono, monospace', fontSize: '12px' } }} />
    </BrowserRouter>
  </React.StrictMode>
);
