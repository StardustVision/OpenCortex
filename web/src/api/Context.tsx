import React, { createContext, useContext, useState } from 'react';
import { OpenCortexClient } from './client';

function decodeJwtPayload(token: string): Record<string, any> {
  try {
    const payload = token.split('.')[1];
    return JSON.parse(atob(payload));
  } catch {
    return {};
  }
}

interface ApiContextType {
  client: OpenCortexClient | null;
  token: string | null;
  role: string;
  setToken: (token: string) => void;
  logout: () => void;
}

const ApiContext = createContext<ApiContextType | undefined>(undefined);

export const ApiProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [token, setTokenState] = useState<string | null>(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
      localStorage.setItem('opencortex_token', urlToken);
      const newUrl = window.location.pathname;
      window.history.replaceState({}, '', newUrl);
      return urlToken;
    }
    return localStorage.getItem('opencortex_token');
  });

  const [role, setRole] = useState<string>(() => {
    if (token) return decodeJwtPayload(token).role || 'user';
    return 'user';
  });

  const [client, setClient] = useState<OpenCortexClient | null>(() => {
    if (token) return new OpenCortexClient('', token);
    return null;
  });

  const setToken = (newToken: string) => {
    localStorage.setItem('opencortex_token', newToken);
    setTokenState(newToken);
    setRole(decodeJwtPayload(newToken).role || 'user');
    setClient(new OpenCortexClient('', newToken));
  };

  const logout = () => {
    localStorage.removeItem('opencortex_token');
    setTokenState(null);
    setRole('user');
    setClient(null);
  };

  return (
    <ApiContext.Provider value={{ client, token, role, setToken, logout }}>
      {children}
    </ApiContext.Provider>
  );
};

export const useApi = () => {
  const context = useContext(ApiContext);
  if (context === undefined) {
    throw new Error('useApi must be used within an ApiProvider');
  }
  return context;
};
