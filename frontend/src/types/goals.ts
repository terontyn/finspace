import type { PageMeta } from "@/types/finance";

export type GoalMoney = string;
export type GoalStatus = "active" | "paused" | "completed" | "cancelled";

export interface Goal {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  currency: string;
  target_amount: GoalMoney;
  target_date: string | null;
  status: GoalStatus;
  version: number;
  deleted_at: string | null;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  contributed_amount: GoalMoney;
  remaining_amount: GoalMoney;
  progress_percent: string;
  is_target_reached: boolean;
  contribution_count: number;
  days_remaining: number | null;
  overdue: boolean;
}

export interface GoalPage {
  items: Goal[];
  page: PageMeta;
}

export interface GoalCreateRequest {
  name: string;
  description: string | null;
  currency: string;
  target_amount: GoalMoney;
  target_date: string | null;
}

export interface GoalUpdateRequest {
  version: number;
  name?: string;
  description?: string | null;
  currency?: string;
  target_amount?: GoalMoney;
  target_date?: string | null;
}

export interface GoalVersionRequest {
  version: number;
}

export interface GoalContribution {
  id: string;
  goal_id: string;
  workspace_id: string;
  currency: string;
  amount: GoalMoney;
  note: string | null;
  contributed_at: string;
  correction_of_id: string | null;
  created_by: string;
  created_by_display_name: string | null;
  created_at: string;
}

export interface GoalContributionPage {
  items: GoalContribution[];
  page: PageMeta;
}

export interface GoalContributionCreateRequest {
  amount: GoalMoney;
  note: string | null;
  contributed_at?: string | null;
}

export interface GoalCorrectionCreateRequest {
  adjustment_amount: GoalMoney;
  note: string | null;
  contributed_at?: string | null;
}

export interface GoalContributionCommandResponse {
  goal: Goal;
  contribution: GoalContribution;
}

export interface GoalListFilters {
  status?: GoalStatus;
  currency?: string;
  includeDeleted?: boolean;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface GoalAuthMeResponse {
  role: "viewer" | "editor" | "owner" | string;
}
