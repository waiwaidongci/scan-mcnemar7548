export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const BATCH = {staging:'待补齐', active:'生效中', superseded:'已取代', failed:'已挂起'};
export const RECEIPT = {staged:'待生效', mismatch:'指纹不符', confirmed:'已确认', superseded:'已随版归档', stale:'乱序已忽略'};
