import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { AdminCaseClosedManage } from '@/pages/AdminCaseClosedManage';

vi.mock('@/services/api', () => ({
  getCaseClosedBlueprints: vi.fn(),
  getCaseClosedPackages: vi.fn(),
  createCaseClosedBlueprint: vi.fn(),
  generateCaseClosedBlueprint: vi.fn(),
  approveCaseClosedBlueprint: vi.fn(),
  generateCaseClosedPackage: vi.fn(),
  publishCaseClosedPackage: vi.fn(),
}));

vi.mock('@/components/AdminGameStatusControls', () => ({ AdminGameStatusControls: () => <div /> }));
vi.mock('@/components/AdminSetFeaturedCard', () => ({ AdminSetFeaturedCard: () => <div /> }));
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }), Toaster: () => null }));

const api = await import('@/services/api');
const getBPs = api.getCaseClosedBlueprints as ReturnType<typeof vi.fn>;
const getPkgs = api.getCaseClosedPackages as ReturnType<typeof vi.fn>;
const createBP = api.createCaseClosedBlueprint as ReturnType<typeof vi.fn>;
const genBP = api.generateCaseClosedBlueprint as ReturnType<typeof vi.fn>;
const publishPkg = api.publishCaseClosedPackage as ReturnType<typeof vi.fn>;

describe('Admin Case Closed', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getBPs.mockResolvedValue([]);
    getPkgs.mockResolvedValue([]);
  });

  it('creates a draft and shows validation errors without publishing invalid', async () => {
    getBPs.mockResolvedValue([
      { id: 'bp1', title: 'Mascot', difficulty: 'medium', config: {}, blueprint: {}, status: 'validation_failed', validation_errors: [{ code: 'cycle', message: 'Cycle a -> b' }], created_by: 'admin', created_at: '', updated_at: '' },
    ]);
    getPkgs.mockResolvedValue([
      { id: 'p1', blueprint_id: 'bp1', version: 1, title: 'Mascot', package: {}, status: 'validation_failed', validation_errors: [{ code: 'unreachable_leads', message: 'Lost lead' }], published_at: null, created_by: 'admin', created_at: '', updated_at: '' },
    ]);
    createBP.mockResolvedValue({ id: 'bp2', title: 'New', difficulty: 'medium', config: {}, blueprint: {}, status: 'draft', validation_errors: [], created_by: 'admin', created_at: '', updated_at: '' });
    render(<MemoryRouter><AdminCaseClosedManage /></MemoryRouter>);
    expect(await screen.findByText(/Blueprints/)).toBeTruthy();
    expect(await screen.findByText(/Cycle a -> b/)).toBeTruthy();
    expect(await screen.findByText(/Lost lead/)).toBeTruthy();
    fireEvent.click(screen.getByText('New draft'));
    await waitFor(() => expect(createBP).toHaveBeenCalled());
  });

  it('publishes only valid packages', async () => {
    getPkgs.mockResolvedValue([
      { id: 'p9', blueprint_id: 'bp1', version: 2, title: 'Mascot', package: {}, status: 'ready_for_review', validation_errors: [], published_at: null, created_by: 'admin', created_at: '', updated_at: '' },
    ]);
    publishPkg.mockResolvedValue({ id: 'p9', blueprint_id: 'bp1', version: 2, title: 'Mascot', package: {}, status: 'published', validation_errors: [], published_at: '', created_by: 'admin', created_at: '', updated_at: '' });
    genBP.mockResolvedValue({});
    render(<MemoryRouter><AdminCaseClosedManage /></MemoryRouter>);
    fireEvent.click(await screen.findByText('Publish'));
    await waitFor(() => expect(publishPkg).toHaveBeenCalledWith('p9'));
  });
});
