import { createContext, useContext } from "react";

export interface Me {
  id: number;
  name: string;
  role: "owner" | "member";
  level: "workspace" | "media";
  evening_digest: boolean;
  morning_brief: boolean;
}

export const MeContext = createContext<Me | null>(null);

/** The logged-in user. Pages render inside App only after /api/auth/me loaded. */
export function useMe(): Me {
  const me = useContext(MeContext);
  if (!me) throw new Error("useMe outside MeContext");
  return me;
}

export function useIsOwner(): boolean {
  return useMe().role === "owner";
}
