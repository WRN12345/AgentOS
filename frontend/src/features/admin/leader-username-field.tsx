import { useWatch } from "react-hook-form";
import type { Control, FieldValues, Path } from "react-hook-form";
import { Input } from "@/components/ui/input";
import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import type { UserMe } from "../../types";
import { resolveUser } from "./resolve-user";

interface LeaderUsernameFieldProps<T extends FieldValues> {
  control: Control<T>;
  name: Path<T>;
  label: string;
  users: UserMe[] | undefined;
}

/** 按完整用户名解析指定/变更负责人的输入字段（含解析提示；无搜索端点）。 */
export function LeaderUsernameField<T extends FieldValues>({
  control,
  name,
  label,
  users,
}: LeaderUsernameFieldProps<T>) {
  const username = (useWatch({ control, name }) as string | undefined) ?? "";
  const { hint } = resolveUser(users, username);
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem>
          <FormLabel>{label}</FormLabel>
          <FormControl>
            <Input autoComplete="off" placeholder="输入完整用户名" {...field} />
          </FormControl>
          {hint &&
            (hint.tone === "ok" ? (
              <p className="text-sm text-emerald-600">{hint.text}</p>
            ) : (
              <p className="text-sm text-destructive">{hint.text}</p>
            ))}
          <FormMessage />
        </FormItem>
      )}
    />
  );
}
