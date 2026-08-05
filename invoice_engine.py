"""HireLab invoice engine — builds the exact Tally-style GST tax-invoice HTML
for browser Print/Save-as-PDF. Standalone (no system deps). Imported by server.py."""

INVOICE_LOGO_B64 = "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"

_ONES = ['', 'One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten',
         'Eleven','Twelve','Thirteen','Fourteen','Fifteen','Sixteen','Seventeen',
         'Eighteen','Nineteen']
_TENS = ['','','Twenty','Thirty','Forty','Fifty','Sixty','Seventy','Eighty','Ninety']

def _two(n):
    if n < 20: return _ONES[n]
    return _TENS[n//10] + ((' ' + _ONES[n%10]) if n%10 else '')

def _three(n):
    h, r = divmod(n, 100); out = ''
    if h: out += _ONES[h] + ' Hundred' + (' ' if r else '')
    if r: out += _two(r)
    return out

def _num_in(n):
    n = int(n)
    if n == 0: return 'Zero'
    cr, n = divmod(n, 10**7); la, n = divmod(n, 10**5); th, n = divmod(n, 1000)
    parts = []
    if cr: parts.append(_three(cr) + ' Crore')
    if la: parts.append(_two(la) + ' Lakh')
    if th: parts.append(_two(th) + ' Thousand')
    if n: parts.append(_three(n))
    return ' '.join(parts).strip()

def rupees_words(amount):
    r = int(amount); p = int(round((amount - r) * 100))
    s = 'INR ' + _num_in(r)
    if p: s += ' and ' + _num_in(p) + ' paise'
    return s + ' Only'

def tax_words(amount):
    r = int(amount); p = int(round((amount - r) * 100))
    s = 'INR ' + _num_in(r)
    if p: s += ' and ' + _num_in(p) + ' paise'
    else: s += ' Rupees'
    return s + ' Only'

def inr(x):
    neg = x < 0; x = abs(round(x, 2)); whole = int(x); dec = int(round((x - whole) * 100))
    s = str(whole)
    if len(s) > 3:
        last3 = s[-3:]; rest = s[:-3]; groups = []
        while len(rest) > 2: groups.insert(0, rest[-2:]); rest = rest[:-2]
        if rest: groups.insert(0, rest)
        s = ','.join(groups) + ',' + last3
    out = "%s.%02d" % (s, dec)
    return ('-' + out) if neg else out

def _esc(s):
    return (str(s if s is not None else '')).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


def build_invoice_html(d, for_print=True):
    seller = d['seller']; buyer = d['buyer']; con = d.get('consignee') or buyer
    inter_state = str(seller.get('state_code','')) != str(buyer.get('state_code',''))
    gst_rate = float(d.get('gst_rate', 18) or 18)
    taxable = round(float(d.get('amount', 0) or 0), 2)
    if inter_state:
        igst = round(taxable * gst_rate / 100, 2); cgst = sgst = 0.0; tax_total = igst
    else:
        cgst = round(taxable * (gst_rate/2) / 100, 2); sgst = cgst; igst = 0.0; tax_total = cgst + sgst
    gross = taxable + tax_total; rounded = round(gross); round_off = round(rounded - gross, 2)

    cand_html = ''
    if d.get('candidate_name'):
        role = (' — ' + _esc(d['role'])) if d.get('role') else ''
        cand_html = '<div style="font-weight:normal;font-size:9.5pt;margin-top:2px">Candidate: ' + _esc(d['candidate_name']) + role + '</div>'
    extra_html = ''
    for ln in (d.get('extra_lines') or []):
        if str(ln).strip():
            extra_html += '<div style="font-size:9.5pt;margin-top:3px">' + _esc(ln) + '</div>'

    if inter_state:
        tax_lines = '<div>IGST</div>'; tax_amt_lines = '<div>' + inr(igst) + '</div>'
    else:
        tax_lines = '<div>CGST</div><div>SGST</div>'
        tax_amt_lines = '<div>' + inr(cgst) + '</div><div>' + inr(sgst) + '</div>'
    ro_line = '<div>Rounded Off</div>' if abs(round_off) >= 0.005 else ''
    ro_amt = '<div>' + inr(round_off) + '</div>' if abs(round_off) >= 0.005 else ''

    if inter_state:
        s_head = '<th>Taxable<br>Value</th><th>IGST<br>Rate</th><th>IGST<br>Amount</th><th>Total<br>Tax Amount</th>'
        s_row = '<td class="r">'+inr(taxable)+'</td><td class="c">'+('%g'%gst_rate)+'%</td><td class="r">'+inr(igst)+'</td><td class="r">'+inr(tax_total)+'</td>'
        s_tot = '<td class="r b">'+inr(taxable)+'</td><td></td><td class="r b">'+inr(igst)+'</td><td class="r b">'+inr(tax_total)+'</td>'
    else:
        half = '%g' % (gst_rate/2)
        s_head = '<th>Taxable<br>Value</th><th>CGST<br>Rate</th><th>CGST<br>Amount</th><th>SGST<br>Rate</th><th>SGST<br>Amount</th><th>Total<br>Tax Amount</th>'
        s_row = '<td class="r">'+inr(taxable)+'</td><td class="c">'+half+'%</td><td class="r">'+inr(cgst)+'</td><td class="c">'+half+'%</td><td class="r">'+inr(sgst)+'</td><td class="r">'+inr(tax_total)+'</td>'
        s_tot = '<td class="r b">'+inr(taxable)+'</td><td></td><td class="r b">'+inr(cgst)+'</td><td></td><td class="r b">'+inr(sgst)+'</td><td class="r b">'+inr(tax_total)+'</td>'

    copy_label = d.get('copy_label', 'ORIGINAL FOR RECIPIENT')
    total_qty = _esc(d.get('total_qty','')).replace(chr(10), '<br>')
    print_bar = ''
    if for_print:
        print_bar = ('<div class="noprint" style="text-align:center;padding:10px;background:#f3f2fb">'
                     '<button onclick="window.print()" style="background:#534AB7;color:#fff;border:none;padding:9px 22px;'
                     'border-radius:8px;font-size:14px;cursor:pointer">&#128424; Print / Save as PDF</button></div>')

    return ('<!doctype html><html><head><meta charset="utf-8"><title>Invoice ' + _esc(d.get('invoice_no','')) + '</title><style>'
      '@page { size: A4; margin: 10mm 8mm; }'
      '* { box-sizing: border-box; } html,body { margin:0; padding:0; }'
      "body { font-family:'Helvetica','Arial',sans-serif; font-size:9.5pt; color:#000; background:#e9e9ef; }"
      '.sheet { width:210mm; min-height:297mm; margin:10px auto; background:#fff; padding:10mm 8mm; }'
      '@media print { body{background:#fff;} .sheet{margin:0;width:auto;padding:0;} .noprint{display:none!important;} }'
      '.title { text-align:center; font-weight:bold; font-size:13pt; }'
      '.copy { position:absolute; right:0; top:2px; font-style:italic; font-size:9pt; }'
      'table.inv { width:100%; border-collapse:collapse; }'
      '.inv td, .inv th { border:0.6pt solid #000; padding:3px 5px; vertical-align:top; }'
      '.nob { border:none !important; } .c{text-align:center;} .r{text-align:right;} .b{font-weight:bold;}'
      '.sm { font-size:8.5pt; } .lh{line-height:1.25;} .seller-name{font-weight:bold;font-size:11pt;}'
      '.meta td { padding:2px 5px; font-size:9pt; } .hdr th{font-weight:bold;text-align:center;font-size:9pt;}'
      '.words{font-weight:bold;} .foot{text-align:center;font-size:8.5pt;margin-top:6px;}'
      '</style></head><body>' + print_bar + '<div class="sheet">'
      '<div style="position:relative"><div class="title">Tax Invoice</div><div class="copy">(' + _esc(copy_label) + ')</div></div>'
      '<table class="inv" style="margin-top:4px"><tr>'
        '<td style="width:55%" class="lh"><table class="nob" style="width:100%"><tr>'
          '<td class="nob" style="width:120px;vertical-align:top"><img src="data:image/png;base64,' + INVOICE_LOGO_B64 + '" style="width:110px"></td>'
          '<td class="nob"><div class="seller-name">' + _esc(seller['name']) + '</div>'
          '<div class="b sm">Address</div><div class="sm">' + _esc(seller.get('address','')) + '</div>'
          '<div class="sm">' + _esc(seller.get('udyam','')) + '</div>'
          '<div class="sm">GSTIN/UIN: ' + _esc(seller.get('gstin','')) + '</div>'
          '<div class="sm">State Name : ' + _esc(seller.get('state','')) + ', Code : ' + _esc(seller.get('state_code','')) + '</div>'
        '</td></tr></table></td>'
        '<td style="width:45%;padding:0"><table class="inv meta" style="width:100%;height:100%">'
          '<tr><td style="width:50%">Invoice No.<div class="b">' + _esc(d.get('invoice_no','')) + '</div></td><td>Dated<div class="b">' + _esc(d.get('invoice_date','')) + '</div></td></tr>'
          '<tr><td>Reference No. &amp; Date.<div class="b">' + _esc(d.get('ref_no','')) + '</div></td><td>Other References<div class="b">' + _esc(d.get('other_ref','')) + '</div></td></tr>'
          '<tr><td>Buyer&#39;s Order No.<div class="b">' + _esc(d.get('order_no','')) + '</div></td><td>Dated<div class="b">' + _esc(d.get('order_date','')) + '</div></td></tr>'
          '<tr><td style="height:14px"></td><td></td></tr>'
        '</table></td></tr>'
        '<tr><td colspan="2" class="lh"><div>Consignee (Ship to)</div><div class="b">' + _esc(con['name']) + '</div>'
          '<div class="sm">' + _esc(con.get('address','')) + '</div><div class="sm">GSTIN/UIN&nbsp;&nbsp;&nbsp;: ' + _esc(con.get('gstin','')) + '</div>'
          '<div class="sm">State Name&nbsp;&nbsp;: ' + _esc(con.get('state','')) + ', Code : ' + _esc(con.get('state_code','')) + '</div></td></tr>'
        '<tr><td colspan="2" class="lh"><div>Buyer (Bill to)</div><div class="b">' + _esc(buyer['name']) + '</div>'
          '<div class="sm">' + _esc(buyer.get('address','')) + '</div><div class="sm">GSTIN/UIN&nbsp;&nbsp;&nbsp;: ' + _esc(buyer.get('gstin','')) + '</div>'
          '<div class="sm">State Name&nbsp;&nbsp;: ' + _esc(buyer.get('state','')) + ', Code : ' + _esc(buyer.get('state_code','')) + '</div>'
          '<div class="sm">Place of Supply : ' + _esc(d.get('place_of_supply', buyer.get('state',''))) + '</div></td></tr>'
      '</table>'
      '<table class="inv" style="margin-top:-0.6pt"><tr class="hdr">'
        '<th style="width:4%">Sl<br>No.</th><th style="width:40%">Description of<br>Services</th><th style="width:11%">HSN/SAC</th>'
        '<th style="width:13%">Quantity</th><th style="width:11%">Rate</th><th style="width:6%">per</th><th style="width:15%">Amount</th></tr>'
        '<tr><td class="c" style="height:230px;vertical-align:top">1</td>'
        '<td style="vertical-align:top"><div class="b">' + _esc(d.get('description','')) + '</div>' + cand_html + extra_html +
          '<div style="text-align:right;margin-top:70px" class="b">' + tax_lines + ro_line + '</div></td>'
        '<td class="c" style="vertical-align:top">' + _esc(d.get('hsn','998512')) + '</td>'
        '<td class="r b" style="vertical-align:top">' + _esc(d.get('quantity','')) + '</td>'
        '<td class="r" style="vertical-align:top">' + _esc(d.get('rate','')) + '</td>'
        '<td class="c" style="vertical-align:top">' + _esc(d.get('per','CTC')) + '</td>'
        '<td class="r" style="vertical-align:top"><div class="b">' + inr(taxable) + '</div><div style="margin-top:70px">' + tax_amt_lines + ro_amt + '</div></td></tr>'
        '<tr><td></td><td class="r b">Total</td><td></td><td class="r b">' + total_qty + '</td><td></td><td></td>'
        '<td class="r b" style="font-size:11pt">&#8377;&nbsp;' + inr(rounded) + '</td></tr></table>'
      '<table class="inv" style="margin-top:-0.6pt"><tr><td class="nob" style="border:0.6pt solid #000">'
        '<div>Amount Chargeable (in words)</div><div class="words">' + rupees_words(rounded) + '</div></td>'
        '<td class="nob r" style="border:0.6pt solid #000;width:12%;vertical-align:bottom">E. &amp; O.E</td></tr></table>'
      '<table class="inv" style="margin-top:-0.6pt"><tr class="hdr"><th style="border-left:none"></th>' + s_head + '</tr>'
        '<tr><td style="border-left:none"></td>' + s_row + '</tr>'
        '<tr><td class="r b" style="border-left:none">Total:</td>' + s_tot + '</tr></table>'
      '<table class="inv" style="margin-top:-0.6pt"><tr><td colspan="2">'
        '<div>Tax Amount (in words) : <span class="b">' + tax_words(tax_total) + '</span></div>'
        '<div class="b">Declaration</div>'
        '<div class="sm">We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.</div></td></tr>'
        '<tr><td style="width:55%;height:70px;vertical-align:top">Customer&#39;s Seal and Signature</td>'
        '<td style="vertical-align:top;text-align:right"><div class="b">for ' + _esc(seller['name']) + '</div>'
        '<div style="height:44px;text-align:right;color:#1a3a6b;font-style:italic;font-size:13pt;padding-top:6px">' + _esc(d.get('signatory_name','')) + '</div>'
        '<div class="b">Authorised Signatory</div></td></tr></table>'
      '<div class="foot">Registered Office: ' + _esc(seller.get('reg_office','')) + '</div>'
      '<div class="foot">This is a Computer Generated Invoice</div>'
      '</div></body></html>')
