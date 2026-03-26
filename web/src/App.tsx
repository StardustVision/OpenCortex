import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useApi } from './api/Context';
import { Dashboard } from './pages/Dashboard';
import { Memories } from './pages/Memories';
import { Knowledge } from './pages/Knowledge';
import { SearchDebug } from './pages/SearchDebug';
import { System } from './pages/System';
import { Skills } from './pages/Skills';
import { Tokens } from './pages/Tokens';
import { Connect } from './pages/Connect';

export const App: React.FC = () => {
  const { token } = useApi();

  if (!token) {
    return <Connect />;
  }

  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/memories" element={<Memories />} />
      <Route path="/knowledge" element={<Knowledge />} />
      <Route path="/search-debug" element={<SearchDebug />} />
      <Route path="/system" element={<System />} />
      <Route path="/skills" element={<Skills />} />
      <Route path="/tokens" element={<Tokens />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
};
