import React, { useState } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { Modal } from '../components/common/Modal';
import { useApi } from '../api/Context';
import { useFetch } from '../hooks/useFetch';
import { 
  ShieldAlert, 
  Activity, 
  BarChart3, 
  RefreshCw,
  Clock
} from 'lucide-react';

const DoctorCard: React.FC<{ label: string; value: any }> = ({ label, value }) => {
  const isOk = value === true || (typeof value === 'string' && value.length > 0) || (typeof value === 'object' && value !== null);
  const display = typeof value === 'boolean'
    ? (value ? 'OK' : 'Unavailable')
    : typeof value === 'string'
    ? value
    : typeof value === 'object' && value !== null
    ? Object.entries(value).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('\n')
    : String(value);

  return (
    <Card className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="font-bold text-gray-900">{label}</span>
        <div className={`w-3 h-3 rounded-full ${isOk ? 'bg-green-500' : 'bg-red-500'}`} />
      </div>
      <pre className="text-sm text-gray-500 whitespace-pre-wrap font-sans">{display}</pre>
    </Card>
  );
};

export const System: React.FC = () => {
  const { client } = useApi();
  const [isReembedModalOpen, setIsReembedModalOpen] = useState(false);
  const [isDecayModalOpen, setIsDecayModalOpen] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);

  const { data: doctor, loading: doctorLoading, refetch: refetchDoctor } = useFetch(
    () => client!.getDoctor()
  );

  const { data: stats, loading: statsLoading, refetch: refetchStats } = useFetch(
    () => client!.getSystemStats()
  );

  const onRefresh = () => {
    refetchDoctor();
    refetchStats();
  };

  const handleReembed = async () => {
    if (!client) return;
    setActionLoading(true);
    try {
      await client.reembedAll();
      setIsReembedModalOpen(false);
      // Show success toast
    } catch (error) {
      console.error('Re-embed failed', error);
    } finally {
      setActionLoading(false);
    }
  };

  const handleDecay = async () => {
    if (!client) return;
    setActionLoading(true);
    try {
      await client.decayMemories();
      setIsDecayModalOpen(false);
      // Show success toast
    } catch (error) {
      console.error('Decay failed', error);
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <PageLayout title="System" onRefresh={onRefresh} isLoading={doctorLoading || statsLoading}>
      <div className="space-y-8">
        {/* Doctor Report */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <Activity size={20} className="text-gray-400" />
            <h2 className="text-lg font-bold text-gray-900">Doctor Report</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {doctorLoading ? (
              <LoadingSpinner />
            ) : doctor ? (
              <>
                <DoctorCard label="Initialized" value={doctor.initialized} />
                <DoctorCard label="Storage" value={doctor.storage} />
                <DoctorCard label="Embedder" value={doctor.embedder} />
                <DoctorCard label="LLM" value={doctor.llm} />
                <DoctorCard label="Rerank" value={doctor.rerank} />
                {doctor.issues?.length > 0 && (
                  <Card className="flex flex-col gap-2 md:col-span-2 lg:col-span-3 border-red-200">
                    <span className="font-bold text-red-600">Issues</span>
                    <ul className="text-sm text-red-500 list-disc list-inside">
                      {doctor.issues.map((issue: string, i: number) => <li key={i}>{issue}</li>)}
                    </ul>
                  </Card>
                )}
              </>
            ) : null}
          </div>
        </section>

        {/* Storage Stats */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <BarChart3 size={20} className="text-gray-400" />
            <h2 className="text-lg font-bold text-gray-900">Storage Statistics</h2>
          </div>
          <Card className="p-0 overflow-hidden">
            <table className="w-full text-left">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="px-6 py-3 text-xs font-semibold text-gray-500 uppercase">Metric</th>
                  <th className="px-6 py-3 text-xs font-semibold text-gray-500 uppercase">Value</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {statsLoading ? (
                  <tr><td colSpan={2}><LoadingSpinner /></td></tr>
                ) : stats ? (
                  Object.entries(stats).map(([key, value]) => (
                    <tr key={key}>
                      <td className="px-6 py-4 text-sm font-medium text-gray-600">{key}</td>
                      <td className="px-6 py-4 text-sm font-mono text-gray-900">
                        {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                      </td>
                    </tr>
                  ))
                ) : null}
              </tbody>
            </table>
          </Card>
        </section>

        {/* Admin Operations */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <ShieldAlert size={20} className="text-red-500" />
            <h2 className="text-lg font-bold text-gray-900">Danger Zone</h2>
          </div>
          <Card className="border-l-4 border-l-red-500 space-y-6">
            <div className="flex items-center justify-between gap-8">
              <div className="space-y-1">
                <h4 className="text-md font-bold text-gray-900">Re-embed All Records</h4>
                <p className="text-sm text-gray-500">
                  Re-generate vector embeddings for all records using the current model. This is a long-running operation that may impact performance.
                </p>
              </div>
              <Button variant="outline" className="text-red-600 border-red-200 hover:bg-red-50 shrink-0" onClick={() => setIsReembedModalOpen(true)}>
                <RefreshCw size={16} className="mr-2" /> Re-embed All
              </Button>
            </div>

            <div className="flex items-center justify-between gap-8 pt-6 border-t border-gray-100">
              <div className="space-y-1">
                <h4 className="text-md font-bold text-gray-900">Apply Time Decay</h4>
                <p className="text-sm text-gray-500">
                  Manually apply time-decay to all memory scores. Memories below the archive threshold will be moved to long-term storage.
                </p>
              </div>
              <Button variant="outline" className="text-red-600 border-red-200 hover:bg-red-50 shrink-0" onClick={() => setIsDecayModalOpen(true)}>
                <Clock size={16} className="mr-2" /> Run Decay
              </Button>
            </div>
          </Card>
        </section>
      </div>

      {/* Confirmation Modals */}
      <Modal
        isOpen={isReembedModalOpen}
        onClose={() => setIsReembedModalOpen(false)}
        title="Confirm Re-embedding"
        footer={
          <>
            <Button variant="ghost" onClick={() => setIsReembedModalOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={handleReembed} loading={actionLoading}>Start Re-embedding</Button>
          </>
        }
      >
        <p className="text-gray-600">
          This will re-embed all records in the database. Are you sure you want to proceed?
        </p>
      </Modal>

      <Modal
        isOpen={isDecayModalOpen}
        onClose={() => setIsDecayModalOpen(false)}
        title="Confirm Decay"
        footer={
          <>
            <Button variant="ghost" onClick={() => setIsDecayModalOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={handleDecay} loading={actionLoading}>Run Decay</Button>
          </>
        }
      >
        <p className="text-gray-600">
          This will apply time-decay to all memory scores immediately. Are you sure?
        </p>
      </Modal>
    </PageLayout>
  );
};
