import type {
  FactoryBlueprintSummary,
  SoftwareSpecification,
  SpecEntity,
  SpecPermission,
} from '@/api/types';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

export interface DesignedApiEndpoint {
  id: string;
  moduleId: string;
  entityName: string;
  method: HttpMethod;
  path: string;
  summary: string;
  roles: string[];
  requestSummary: string;
  responseSummary: string;
  ruleIds: string[];
  source: 'spec' | 'blueprint';
}

export interface DesignedModuleGroup {
  moduleId: string;
  phase: 'backend' | 'frontend' | 'spec';
  endpoints: DesignedApiEndpoint[];
}

function toKebabPlural(name: string): string {
  const kebab = name
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[\s_]+/g, '-')
    .toLowerCase();
  if (!kebab) return 'items';
  if (kebab.endsWith('y') && !/[aeiou]y$/.test(kebab)) {
    return `${kebab.slice(0, -1)}ies`;
  }
  if (kebab.endsWith('s')) return kebab;
  return `${kebab}s`;
}

function rolesForAction(permissions: SpecPermission[], action: string): string[] {
  const matched = permissions
    .filter((p) => p.actions.includes(action))
    .map((p) => p.role);
  if (matched.length > 0) {
    return Array.from(new Set(matched));
  }
  return ['ADMIN'];
}

function summarizeFields(entity: SpecEntity, includeId: boolean): string {
  const fields = entity.fields.filter((f) => includeId || f.name.toLowerCase() !== 'id');
  if (fields.length === 0) return '—';
  return fields
    .slice(0, 6)
    .map((f) => `${f.name}:${f.type}${f.required ? '*' : ''}`)
    .join(', ');
}

function matchBlueprintModule(entityName: string, blueprints: FactoryBlueprintSummary[]): string {
  const lower = entityName.toLowerCase();
  for (const bp of blueprints) {
    if (bp.phase !== 'backend') continue;
    for (const mod of bp.module_ids) {
      const modNorm = mod.toLowerCase().replace(/[-_]/g, '');
      const entNorm = lower.replace(/[-_]/g, '');
      if (modNorm.includes(entNorm) || entNorm.includes(modNorm)) {
        return mod;
      }
    }
  }
  return toKebabPlural(entityName).replace(/ies$/, 'y').replace(/s$/, '') || lower;
}

/**
 * 基于需求分析产出的 SoftwareSpecification（实体、权限、业务规则、页面）与工厂蓝图摘要，
 * 确定性推导出面向用户的 REST 设计接口清单（供右侧边栏「设计接口」面板展示）。
 */
export function synthesizeDesignedApis(
  spec: SoftwareSpecification | null | undefined,
  blueprints: FactoryBlueprintSummary[] = [],
): DesignedApiEndpoint[] {
  if (!spec) {
    return [];
  }

  const endpoints: DesignedApiEndpoint[] = [];
  const rules = spec.business_rules ?? [];
  const permissions = spec.permissions ?? [];
  const hasBlueprint = blueprints.some((bp) => bp.phase === 'backend' && bp.module_ids.length > 0);
  const source: 'spec' | 'blueprint' = hasBlueprint ? 'blueprint' : 'spec';

  for (const entity of spec.entities ?? []) {
    const cleanName = entity.name.trim();
    if (!cleanName) continue;
    const slug = toKebabPlural(cleanName);
    const modId = matchBlueprintModule(cleanName, blueprints);
    const matchedRules = rules
      .filter((r) => r.text.toLowerCase().includes(cleanName.toLowerCase()))
      .map((r) => r.id);
    const createFields = summarizeFields(entity, false);
    const viewFields = summarizeFields(entity, true);

    endpoints.push(
      {
        id: `${modId}-list`,
        moduleId: modId,
        entityName: cleanName,
        method: 'GET',
        path: `/api/v1/${slug}`,
        summary: `分页查询 ${cleanName} 列表`,
        roles: rolesForAction(permissions, 'read'),
        requestSummary: 'page, size, keyword?',
        responseSummary: `Page<${cleanName}VO> (${viewFields})`,
        ruleIds: matchedRules,
        source,
      },
      {
        id: `${modId}-get`,
        moduleId: modId,
        entityName: cleanName,
        method: 'GET',
        path: `/api/v1/${slug}/{id}`,
        summary: `获取单个 ${cleanName} 详情`,
        roles: rolesForAction(permissions, 'read'),
        requestSummary: 'path: id',
        responseSummary: `${cleanName}VO`,
        ruleIds: matchedRules,
        source,
      },
      {
        id: `${modId}-create`,
        moduleId: modId,
        entityName: cleanName,
        method: 'POST',
        path: `/api/v1/${slug}`,
        summary: `新增 ${cleanName} 记录`,
        roles: rolesForAction(permissions, 'create'),
        requestSummary: createFields,
        responseSummary: `${cleanName}VO (201)`,
        ruleIds: matchedRules,
        source,
      },
      {
        id: `${modId}-update`,
        moduleId: modId,
        entityName: cleanName,
        method: 'PUT',
        path: `/api/v1/${slug}/{id}`,
        summary: `更新 ${cleanName} 信息`,
        roles: rolesForAction(permissions, 'update'),
        requestSummary: `path: id · ${createFields}`,
        responseSummary: `${cleanName}VO`,
        ruleIds: matchedRules,
        source,
      },
      {
        id: `${modId}-delete`,
        moduleId: modId,
        entityName: cleanName,
        method: 'DELETE',
        path: `/api/v1/${slug}/{id}`,
        summary: `删除指定 ${cleanName}`,
        roles: rolesForAction(permissions, 'delete'),
        requestSummary: 'path: id',
        responseSummary: '{ ok: true }',
        ruleIds: matchedRules,
        source,
      },
    );
  }

  // 若存在认证/角色配置，补充标准鉴权接口组
  if ((spec.actors ?? []).length > 0 || permissions.length > 0) {
    const authMod = blueprints
      .filter((b) => b.phase === 'backend')
      .flatMap((b) => b.module_ids)
      .find((m) => /auth|user|member|account/i.test(m)) ?? 'auth';
    endpoints.push(
      {
        id: `${authMod}-login`,
        moduleId: authMod,
        entityName: 'Auth',
        method: 'POST',
        path: '/api/v1/auth/login',
        summary: '用户身份认证与会话签发',
        roles: ['PUBLIC'],
        requestSummary: 'username:string*, password:string*',
        responseSummary: 'SessionTokenVO',
        ruleIds: [],
        source,
      },
      {
        id: `${authMod}-me`,
        moduleId: authMod,
        entityName: 'Auth',
        method: 'GET',
        path: '/api/v1/auth/me',
        summary: '查询当前登录角色与权限集合',
        roles: spec.actors.length ? spec.actors : ['ADMIN', 'MEMBER'],
        requestSummary: 'Session Cookie / Bearer',
        responseSummary: 'PrincipalVO',
        ruleIds: [],
        source,
      },
    );
  }

  return endpoints;
}
