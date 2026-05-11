import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useApi } from './api/Context';
import { Dashboard } from './pages/Dashboard';
import { Memories } from './pages/Memories';
import { Tokens } from './pages/Tokens';
import { Connect } from './pages/Connect';

export const App: React.FC = () => {
  const { token, authStatus } = useApi();

  if (authStatus === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 text-sm text-gray-500">
        Verifying token...
      </div>
    );
  }

  if (!token) {
    return <Connect />;
  }

  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/memories" element={<Memories />} />
      <Route path="/tokens" element={<Tokens />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
};
