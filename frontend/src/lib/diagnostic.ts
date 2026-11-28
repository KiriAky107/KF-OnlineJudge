import type { components } from '../api/generated/schema'

export const diagnosticCategories: Record<
  components['schemas']['UbsanFinding']['category'],
  string
> = {
  signed_integer_overflow: '有符号整数溢出',
  division_by_zero: '除以零',
  index_out_of_bounds: '数组下标越界',
  invalid_shift: '非法位移',
}
