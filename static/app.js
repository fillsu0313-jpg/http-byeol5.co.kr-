/* 공통 JS — 테이블 정렬, 모바일 카드 토글 */

// ── 모바일 카드 토글 ──
function toggleCard(el) {
    const detail = el.nextElementSibling;
    const chevron = el.querySelector('.card-chevron');
    if (detail && detail.classList.contains('card-detail')) {
        detail.classList.toggle('hidden');
        if (chevron) chevron.style.transform = detail.classList.contains('hidden') ? '' : 'rotate(180deg)';
    }
}

// ── 테이블 헤더 클릭 정렬 ──
document.addEventListener('DOMContentLoaded', () => {
    const table = document.getElementById('dashboard-table');
    if (!table) return;

    const headers = table.querySelectorAll('th[data-sort]');
    let currentSort = { col: -1, asc: true };

    headers.forEach((th, idx) => {
        th.addEventListener('click', () => {
            const type = th.dataset.sort;
            const asc = currentSort.col === idx ? !currentSort.asc : (type === 'string');
            currentSort = { col: idx, asc };

            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));

            rows.sort((a, b) => {
                const aText = a.children[idx]?.textContent.trim() || '';
                const bText = b.children[idx]?.textContent.trim() || '';

                if (type === 'number') {
                    const aVal = parseNum(aText);
                    const bVal = parseNum(bText);
                    // NULL('-') 값은 항상 뒤로
                    if (aText === '-' && bText === '-') return 0;
                    if (aText === '-') return 1;
                    if (bText === '-') return -1;
                    return asc ? aVal - bVal : bVal - aVal;
                }
                return asc ? aText.localeCompare(bText, 'ko') : bText.localeCompare(aText, 'ko');
            });

            rows.forEach(r => tbody.appendChild(r));

            // 정렬 방향 표시
            headers.forEach(h => h.classList.remove('text-blue-600'));
            th.classList.add('text-blue-600');
        });
    });
});

function parseNum(s) {
    if (!s || s === '-') return -Infinity;
    return parseFloat(s.replace(/,/g, '')) || 0;
}
