'Attribute VB_Name = "GenerateMap_PPA"
' ================================================================
'  GenerateMap_PPA.bas  -  CHFT Map ver 8.0  -  Two-step PPA map workflow
'
'  v8.0 (2026-09-21) changes vs v7.1:
'    - Tile provider: replaced tile.openstreetmap.org with a 3-way
'      Esri ArcGIS fallback chain (World_Topo, World_Street, World_Imagery).
'      Public OSM tiles now 403-block referer-less local file:// loads,
'      and CartoDB / Stadia now require API keys. Esri is keyless, has
'      no strict Referer policy, and auto-falls-back on tileerror.
'    - Geocoding: new __geocode() helper wraps Nominatim with Photon
'      (https://photon.komoot.io) as automatic fallback. If Nominatim
'      returns empty / errors / times-out, Photon (also OSM-based,
'      keyless) is queried. Both single search (searchFix) and bulk
'      geocode now go through this helper.
'    - Version strings bumped across VBA, HTML title, header bar,
'      and MsgBox titles.
'
'  STEP 1  "Prepare Addresses"  -> PrepareAddresses_PPA
'          Reads PPA sheet -> writes PPA_MapAddresses review sheet
'          Yellow Column C = geocode query, edit before step 2
'
'  STEP 2  "Generate Map"       -> GenerateMap_PPA
'          Reads edited addresses -> opens interactive map
'          Pin popup shows each transaction on its own line
'          Last row is the Subject building (pink "S" pin) unless
'          another row is manually set to "S" in the No. column.
'
'  BUTTONS:
'    "Prepare Addresses" -> PrepareAddresses_PPA  (on PPA data sheet)
'    "Generate Map"      -> GenerateMap_PPA        (on PPA_MapAddresses)
'
'  SIDEBAR:
'    - Sort by Pin No. / Name / Price / Transaction count
'    - Checkbox per row to show/hide individual pins
'    - All / None buttons
'    - Clicking a non-subject row draws a route to the subject pin
' ================================================================
Option Explicit

Private Const ADDR_SHEET As String = "PPA_MapAddresses"

' ---- PPA source column DEFAULTS (auto-detected at runtime from header) ----
Private Const DEF_MEM_NO    As Integer = 1
Private Const DEF_CP_PT     As Integer = 5
Private Const DEF_UNIT      As Integer = 6
Private Const DEF_FLOOR     As Integer = 7
Private Const DEF_BLOCK     As Integer = 8
Private Const DEF_PHASE     As Integer = 9
Private Const DEF_BLD_E     As Integer = 10
Private Const DEF_EST_E     As Integer = 12
Private Const DEF_ST_E      As Integer = 14
Private Const DEF_ST_NO_FRM As Integer = 16
Private Const DEF_ST_NO_TO  As Integer = 17
Private Const DEF_CONSIDER  As Integer = 21
Private Const DEF_NAT_INST  As Integer = 0
Private Const DEF_AGREE_DATE    As Integer = 0
Private Const DEF_SALEABLE_AREA As Integer = 0
Private Const DEF_PUSAGE        As Integer = 0
Private Const DEF_OP_DATE       As Integer = 0

' ---- Review-sheet columns (PPA_MapAddresses) -------------------------
Private Const AC_NO    As Integer = 1   ' sequential number  (or "S" for subject)
Private Const AC_NAME  As Integer = 2   ' building display name
Private Const AC_ADDR  As Integer = 3   ' geocode query  <- EDIT HERE
Private Const AC_COUNT As Integer = 4   ' transaction count
Private Const AC_RANGE As Integer = 5   ' consideration range (info)
Private Const AC_TXN   As Integer = 6   ' pipe-delimited txn lines (hidden)
Private Const AC_ROWS  As Integer = 7   ' source row numbers (hidden)

' Delimiter between transactions stored in Column F
Private Const TXN_SEP As String = "^"   ' must not appear in any field value


'=================================================================
'  STEP 1 - PREPARE ADDRESS REVIEW SHEET
'=================================================================
Sub PrepareAddresses_PPA()

    Dim wsSrc     As Worksheet
    Dim wsDst     As Worksheet
    Dim lastRow   As Long
    Dim headerRow As Long
    Dim i As Long, j As Long

    ' Runtime column indices
    Dim COL_MEM_NO    As Integer: COL_MEM_NO = DEF_MEM_NO
    Dim COL_CP_PT     As Integer: COL_CP_PT = DEF_CP_PT
    Dim COL_UNIT      As Integer: COL_UNIT = DEF_UNIT
    Dim COL_FLOOR     As Integer: COL_FLOOR = DEF_FLOOR
    Dim COL_BLOCK     As Integer: COL_BLOCK = DEF_BLOCK
    Dim COL_PHASE     As Integer: COL_PHASE = DEF_PHASE
    Dim COL_BLD_E     As Integer: COL_BLD_E = DEF_BLD_E
    Dim COL_EST_E     As Integer: COL_EST_E = DEF_EST_E
    Dim COL_ST_E      As Integer: COL_ST_E = DEF_ST_E
    Dim COL_ST_NO_FRM As Integer: COL_ST_NO_FRM = DEF_ST_NO_FRM
    Dim COL_ST_NO_TO  As Integer: COL_ST_NO_TO = DEF_ST_NO_TO
    Dim COL_CONSIDER   As Integer: COL_CONSIDER = DEF_CONSIDER
    Dim COL_NAT_INST   As Integer: COL_NAT_INST = DEF_NAT_INST
    Dim COL_AGREE_DATE    As Integer: COL_AGREE_DATE = DEF_AGREE_DATE
    Dim COL_SALEABLE_AREA As Integer: COL_SALEABLE_AREA = DEF_SALEABLE_AREA
    Dim COL_PUSAGE        As Integer: COL_PUSAGE = DEF_PUSAGE
    Dim COL_OP_DATE       As Integer: COL_OP_DATE = DEF_OP_DATE
    Dim COL_NET_PSF       As Integer: COL_NET_PSF = 0
    Dim COL_GROSS_PSF     As Integer: COL_GROSS_PSF = 0

    Dim bldE     As String, estE    As String
    Dim stE      As String, stNo    As String
    Dim stNoTo   As String, stNum   As String
    Dim cppt     As String, unit    As String
    Dim flr      As String, blk     As String
    Dim phs      As String, priceRaw As String
    Dim dispName As String, fullAddr As String

    Dim locKey       As String
    Dim locKeys()    As String
    Dim locNames()   As String
    Dim locAddrs()   As String
    Dim locCounts()  As Long
    Dim locTxnText() As String
    Dim locSrcRows() As String
    Dim locPriceMin() As Double
    Dim locPriceMax() As Double
    Dim locHasPrice() As Boolean
    Dim locCount As Long
    Dim found    As Boolean

    Set wsSrc = ActiveSheet

    ' Detect header row
    headerRow = 0
    For i = 1 To 10
        If InStr(1, UCase(CStr(wsSrc.Cells(i, 1).Value)), "MEM_NO") > 0 Then
            headerRow = i: Exit For
        End If
    Next i
    If headerRow = 0 Then headerRow = 1

    ' Auto-detect columns from header
    Dim hc As Integer, hv As String
    For hc = 1 To 60
        hv = UCase(Trim(CStr(wsSrc.Cells(headerRow, hc).Value)))
        Select Case hv
            Case "MEM_NO":     COL_MEM_NO = hc
            Case "CP_PT":      COL_CP_PT = hc
            Case "UNIT":       COL_UNIT = hc
            Case "FLR":        COL_FLOOR = hc
            Case "BLK":        COL_BLOCK = hc
            Case "PHASE":      COL_PHASE = hc
            Case "BLD_E_NAME": COL_BLD_E = hc
            Case "EST_E_NAME": COL_EST_E = hc
            Case "ST_E_NAME":  COL_ST_E = hc
            Case "ST_NO_FRM":  COL_ST_NO_FRM = hc
            Case "ST_NO_TO":   COL_ST_NO_TO = hc
            Case "CONSIDERTN": COL_CONSIDER = hc
            Case "NAT_INST":   COL_NAT_INST = hc
            Case "AGREE_DATE", "REG_DATE", "SALE_DATE", "TRANS_DATE", "DATE"
                COL_AGREE_DATE = hc
        End Select
        If InStr(1, hv, "DATE") > 0 And COL_AGREE_DATE = 0 Then COL_AGREE_DATE = hc
        Select Case hv
            Case "SALEABLE_AREA", "NET_FLOOR_AREA", "GROSS_FLOOR_AREA", _
                 "NET_AREA", "GROSS_AREA", "FLOOR_AREA", "AREA", "GFA", "SA", "TOTNFA"
                If COL_SALEABLE_AREA = 0 Then COL_SALEABLE_AREA = hc
            Case "PUSAGE"
                COL_PUSAGE = hc
            Case "OP_DATE", "OP DATE"
                COL_OP_DATE = hc
            Case "NET_PSF"
                COL_NET_PSF = hc
            Case "GROSS_PSF"
                COL_GROSS_PSF = hc
        End Select
    Next hc

    lastRow = wsSrc.Cells(wsSrc.Rows.Count, COL_MEM_NO).End(xlUp).Row
    If lastRow <= headerRow Then
        MsgBox "No data found.", vbExclamation: Exit Sub
    End If

    locCount = 0
    ReDim locKeys(0):     ReDim locNames(0):    ReDim locAddrs(0)
    ReDim locCounts(0):   ReDim locTxnText(0):  ReDim locSrcRows(0)
    ReDim locPriceMin(0): ReDim locPriceMax(0): ReDim locHasPrice(0)

    For i = headerRow + 1 To lastRow

        bldE = Trim(CStr(wsSrc.Cells(i, COL_BLD_E).Value))
        estE = Trim(CStr(wsSrc.Cells(i, COL_EST_E).Value))
        stE = Trim(CStr(wsSrc.Cells(i, COL_ST_E).Value))
        stNo = Trim(CStr(wsSrc.Cells(i, COL_ST_NO_FRM).Value))
        stNoTo = Trim(CStr(wsSrc.Cells(i, COL_ST_NO_TO).Value))
        cppt = Trim(CStr(wsSrc.Cells(i, COL_CP_PT).Value))
        unit = Trim(CStr(wsSrc.Cells(i, COL_UNIT).Value))
        flr = Trim(CStr(wsSrc.Cells(i, COL_FLOOR).Value))
        blk = Trim(CStr(wsSrc.Cells(i, COL_BLOCK).Value))
        phs = Trim(CStr(wsSrc.Cells(i, COL_PHASE).Value))
        priceRaw = Trim(CStr(wsSrc.Cells(i, COL_CONSIDER).Value))

        Dim natInst   As String
        Dim agreeDate As String
        If COL_NAT_INST > 0 Then
            natInst = Trim(CStr(wsSrc.Cells(i, COL_NAT_INST).Value))
        Else
            natInst = ""
        End If
        If COL_AGREE_DATE > 0 Then
            agreeDate = Trim(CStr(wsSrc.Cells(i, COL_AGREE_DATE).Value))
        Else
            agreeDate = ""
        End If

        Dim saleableArea As String
        If COL_SALEABLE_AREA > 0 Then
            saleableArea = Trim(CStr(wsSrc.Cells(i, COL_SALEABLE_AREA).Value))
        Else
            saleableArea = ""
        End If

        Dim pusage As String
        If COL_PUSAGE > 0 Then
            pusage = Trim(CStr(wsSrc.Cells(i, COL_PUSAGE).Value))
        Else
            pusage = ""
        End If

        Dim opDate As String
        If COL_OP_DATE > 0 Then
            opDate = Trim(CStr(wsSrc.Cells(i, COL_OP_DATE).Value))
        Else
            opDate = ""
        End If

        Dim netPSFRaw   As String
        Dim grossPSFRaw As String
        If COL_NET_PSF > 0 Then
            netPSFRaw = Trim(CStr(wsSrc.Cells(i, COL_NET_PSF).Value))
        Else
            netPSFRaw = ""
        End If
        If COL_GROSS_PSF > 0 Then
            grossPSFRaw = Trim(CStr(wsSrc.Cells(i, COL_GROSS_PSF).Value))
        Else
            grossPSFRaw = ""
        End If
        Dim txnRowNum As Long: txnRowNum = i   ' actual Excel row number

        stNum = stNo
        If stNoTo <> "" And stNoTo <> stNo Then stNum = stNo & "-" & stNoTo

        If bldE <> "" Then
            dispName = bldE
        ElseIf estE <> "" Then
            dispName = estE
        ElseIf stE <> "" Then
            dispName = IIf(stNum <> "", stNum & " " & stE, stE)
        Else
            dispName = "Unknown (Row " & i & ")"
        End If

        If stNum <> "" And stE <> "" Then
            fullAddr = stNum & " " & stE & ", Hong Kong"
        ElseIf stE <> "" Then
            fullAddr = stE & ", Hong Kong"
        Else
            fullAddr = "Hong Kong"
        End If

        Dim txnLine As String
        txnLine = cppt & "|" & unit & "|" & flr & "|" & blk & "|" & phs & "|" & _
                  FormatPrice(priceRaw) & "|" & natInst & "|" & agreeDate & "|" & saleableArea & _
                  "|" & pusage & "|" & opDate & "|" & FormatPSF(grossPSFRaw) & "|" & FormatPSF(netPSFRaw)

        Dim priceNum As Double: priceNum = 0
        On Error Resume Next
        If netPSFRaw <> "" Then
            priceNum = CDbl(netPSFRaw)
        Else
            priceNum = 0
        End If
        On Error GoTo 0

        locKey = UCase(dispName) & "|" & UCase(stE)
        found = False

        For j = 1 To locCount
            If locKeys(j) = locKey Then
                locCounts(j) = locCounts(j) + 1
                locTxnText(j) = locTxnText(j) & TXN_SEP & txnLine
                locSrcRows(j) = locSrcRows(j) & ", " & txnRowNum
                If priceNum > 0 Then
                    If Not locHasPrice(j) Then
                        locPriceMin(j) = priceNum: locPriceMax(j) = priceNum
                        locHasPrice(j) = True
                    Else
                        If priceNum < locPriceMin(j) Then locPriceMin(j) = priceNum
                        If priceNum > locPriceMax(j) Then locPriceMax(j) = priceNum
                    End If
                End If
                found = True: Exit For
            End If
        Next j

        If Not found Then
            locCount = locCount + 1
            ReDim Preserve locKeys(locCount):     ReDim Preserve locNames(locCount)
            ReDim Preserve locAddrs(locCount):    ReDim Preserve locCounts(locCount)
            ReDim Preserve locTxnText(locCount):  ReDim Preserve locSrcRows(locCount)
            ReDim Preserve locPriceMin(locCount): ReDim Preserve locPriceMax(locCount)
            ReDim Preserve locHasPrice(locCount)
            locNames(locCount) = dispName
            locAddrs(locCount) = fullAddr
            locCounts(locCount) = 1
            locKeys(locCount) = locKey
            locTxnText(locCount) = txnLine
            locSrcRows(locCount) = CStr(txnRowNum)
            locHasPrice(locCount) = (priceNum > 0)
            If priceNum > 0 Then
                locPriceMin(locCount) = priceNum
                locPriceMax(locCount) = priceNum
            End If
        End If

    Next i

    If locCount = 0 Then
        MsgBox "No valid locations extracted.", vbExclamation: Exit Sub
    End If

    '---- Create / clear review sheet ---------------------------------
    On Error Resume Next
    Set wsDst = ThisWorkbook.Sheets(ADDR_SHEET)
    On Error GoTo 0
    If wsDst Is Nothing Then
        Set wsDst = ThisWorkbook.Sheets.Add(After:=wsSrc)
        wsDst.Name = ADDR_SHEET
    Else
        wsDst.Cells.Clear
    End If

    wsDst.Columns("A").ColumnWidth = 6
    wsDst.Columns("B").ColumnWidth = 26
    wsDst.Columns("C").ColumnWidth = 50
    wsDst.Columns("D").ColumnWidth = 14
    wsDst.Columns("E").ColumnWidth = 28
    wsDst.Columns("F").ColumnWidth = 4
    wsDst.Columns("G").ColumnWidth = 4

    '---- Row 1: Title ------------------------------------------------
    wsDst.Range("A1:E1").Merge
    With wsDst.Range("A1")
        .Value = "PPA Map  -  Address Review Sheet"
        .Font.Bold = True: .Font.Size = 14
        .Font.Color = RGB(255, 255, 255)
        .Interior.Color = RGB(26, 35, 126)
        .HorizontalAlignment = xlCenter: .VerticalAlignment = xlCenter
    End With
    wsDst.Rows(1).RowHeight = 34

    '---- Row 2: Instructions -----------------------------------------
    wsDst.Range("A2:E2").Merge
    With wsDst.Range("A2")
        .Value = "  Edit GEOCODE ADDRESS (yellow). Last row = Subject building (pink S pin). " & _
                 "Manually set No. column to S to designate a different subject. " & _
                 "Abbreviations: RD C = Road Central | ST = Street | AVE = Avenue"
        .Font.Italic = True: .Font.Size = 10
        .Font.Color = RGB(85, 85, 85)
        .Interior.Color = RGB(255, 248, 225)
        .WrapText = True
    End With
    wsDst.Rows(2).RowHeight = 30

    '---- Row 3: Headers ----------------------------------------------
    Dim hdrs As Variant
    hdrs = Array("No.", "Building / Display Name", "Geocode Address  (EDIT HERE)", "Transactions", "Consideration")
    Dim c As Integer
    For c = 1 To 5
        With wsDst.Cells(3, c)
            .Value = hdrs(c - 1)
            .Font.Bold = True: .Font.Color = RGB(255, 255, 255)
            .Interior.Color = RGB(40, 53, 147)
            .HorizontalAlignment = IIf(c = 1 Or c = 4, xlCenter, xlLeft)
        End With
    Next c
    wsDst.Rows(3).RowHeight = 20

    '---- Data rows ---------------------------------------------------
    Dim fillWhite As Long: fillWhite = RGB(255, 255, 255)
    Dim fillAlt   As Long: fillAlt = RGB(232, 234, 246)
    Dim fillEdit  As Long: fillEdit = RGB(255, 249, 196)
    Dim fillSubj  As Long: fillSubj = RGB(255, 220, 230)    ' pink for subject

    For j = 1 To locCount
        Dim r       As Long: r = j + 3
        Dim isSubj  As Boolean: isSubj = (j = locCount)     ' last row = subject by default
        Dim rowFill As Long
        rowFill = IIf(isSubj, fillSubj, IIf(j Mod 2 = 0, fillAlt, fillWhite))
        wsDst.Rows(r).RowHeight = 22

        Dim priceDisplay As String
        If locHasPrice(j) Then
            If locPriceMin(j) = locPriceMax(j) Then
                priceDisplay = FormatPSF(CStr(locPriceMin(j)))
            Else
                priceDisplay = FormatPSF(CStr(locPriceMin(j))) & " ¨C " & FormatPSF(CStr(locPriceMax(j)))
            End If
        Else
            priceDisplay = "N/A"
        End If

        ' Col A: No. (or "S" for subject)
        With wsDst.Cells(r, AC_NO)
            .Value = IIf(isSubj, "S", j)
            .Font.Bold = True
            .Font.Color = IIf(isSubj, RGB(160, 0, 60), RGB(26, 35, 126))
            .HorizontalAlignment = xlCenter
            .Interior.Color = rowFill
        End With

        With wsDst.Cells(r, AC_NAME)
            .Value = locNames(j): .Interior.Color = rowFill
        End With
        With wsDst.Cells(r, AC_ADDR)
            .Value = locAddrs(j): .Interior.Color = fillEdit
        End With
        With wsDst.Cells(r, AC_COUNT)
            .Value = locCounts(j)
            .HorizontalAlignment = xlCenter
            .Interior.Color = rowFill
        End With
        With wsDst.Cells(r, AC_RANGE)
            .Value = priceDisplay
            .Font.Color = RGB(198, 40, 40): .Font.Bold = True
            .Interior.Color = rowFill
        End With

        wsDst.Cells(r, AC_TXN).Value = locTxnText(j)
        wsDst.Cells(r, AC_ROWS).Value = locSrcRows(j)

        With wsDst.Range(wsDst.Cells(r, 1), wsDst.Cells(r, 5)).Borders(xlEdgeBottom)
            .LineStyle = xlContinuous
            .Color = RGB(189, 189, 189): .Weight = xlThin
        End With
    Next j

    wsDst.Columns("F:G").Hidden = True

    '---- Note row ----------------------------------------------------
    Dim nr As Long: nr = locCount + 4
    wsDst.Range(wsDst.Cells(nr, 1), wsDst.Cells(nr, 5)).Merge
    With wsDst.Cells(nr, 1)
        .Value = "  Common HK abbreviations:  " & _
                 "RD = Road  |  ST = Street  |  C = Central  |  W = West  |  E = East  |  " & _
                 "N = North  |  AVE = Avenue  |  PL = Place  |  CTR = Centre  |  HSE = House"
        .Font.Italic = True: .Font.Size = 9
        .Font.Color = RGB(100, 100, 100)
        .Interior.Color = RGB(240, 240, 240)
    End With
    wsDst.Rows(nr).RowHeight = 20

    wsDst.Activate
    ActiveWindow.FreezePanes = False
    wsDst.Range("A4").Select
    ActiveWindow.FreezePanes = True

    MsgBox "Done!  " & locCount & " unique location(s) written to """ & ADDR_SHEET & """." & vbNewLine & vbNewLine & _
           "The LAST row is marked as Subject (pink, S pin) by default." & vbNewLine & _
           "To change subject: set any row's No. column to S manually." & vbNewLine & vbNewLine & _
           "1. Review the yellow Geocode Address column (Column C)." & vbNewLine & _
           "2. Fix abbreviations  (e.g. RD C -> Road Central)." & vbNewLine & _
           "3. Click the Generate Map button when ready.", _
           vbInformation, "Step 1 Complete - CHFT Map ver 8.0"

End Sub


'=================================================================
'  STEP 2 - GENERATE MAP FROM REVIEW SHEET
'=================================================================
Sub GenerateMap_PPA()

    Dim ws       As Worksheet
    Dim lastRow  As Long
    Dim i        As Long
    Dim jsLocs   As String
    Dim locCount As Long
    Dim html     As String
    Dim filePath As String

    On Error Resume Next
    Set ws = ThisWorkbook.Sheets(ADDR_SHEET)
    On Error GoTo 0
    If ws Is Nothing Then
        MsgBox "Sheet """ & ADDR_SHEET & """ not found." & vbNewLine & _
               "Please run Prepare Addresses first.", vbExclamation
        Exit Sub
    End If

    Const DATA_START As Integer = 4
    lastRow = ws.Cells(ws.Rows.Count, AC_NO).End(xlUp).Row
    If lastRow < DATA_START Then
        MsgBox "No data in """ & ADDR_SHEET & """.", vbExclamation: Exit Sub
    End If

    ' Detect which row is subject (value = "S")
    Dim subjRow As Long: subjRow = lastRow   ' default = last row
    Dim r2 As Long
    For r2 = DATA_START To lastRow
        If UCase(Trim(CStr(ws.Cells(r2, AC_NO).Value))) = "S" Then
            subjRow = r2: Exit For
        End If
    Next r2

    locCount = 0
    jsLocs = "var locations = [" & vbNewLine

    For i = DATA_START To lastRow
        Dim noVal   As String: noVal = Trim(CStr(ws.Cells(i, AC_NO).Value))
        Dim nm      As String: nm = Trim(CStr(ws.Cells(i, AC_NAME).Value))
        Dim addr    As String: addr = Trim(CStr(ws.Cells(i, AC_ADDR).Value))
        Dim cnt     As String: cnt = Trim(CStr(ws.Cells(i, AC_COUNT).Value))
        Dim prng    As String: prng = Trim(CStr(ws.Cells(i, AC_RANGE).Value))
        Dim txnRaw  As String: txnRaw = Trim(CStr(ws.Cells(i, AC_TXN).Value))
        Dim srcRows As String: srcRows = Trim(CStr(ws.Cells(i, AC_ROWS).Value))
        Dim isSubj  As String: isSubj = IIf(i = subjRow, "1", "0")

        If addr = "" Then GoTo SkipRow

        ' If noVal is blank use locCount+1; if "S" keep "S"
        If noVal = "" Then noVal = CStr(locCount + 1)

        Dim txnJS As String
        txnJS = JSEsc(Replace(txnRaw, TXN_SEP, "~~~"))

        jsLocs = jsLocs & _
            "  {no:'" & JSEsc(noVal) & "',name:'" & JSEsc(nm) & "',address:'" & JSEsc(addr) & _
            "',count:'" & JSEsc(cnt) & "',priceRange:'" & JSEsc(prng) & _
            "',srcRows:'" & JSEsc(srcRows) & "',isSubject:" & isSubj & _
            ",txnText:'" & txnJS & "'}," & vbNewLine
        locCount = locCount + 1
SkipRow:
    Next i

    jsLocs = jsLocs & "];"

    If locCount = 0 Then
        MsgBox "No valid rows found.", vbExclamation: Exit Sub
    End If

    html = BuildHTML(jsLocs, locCount)
    filePath = Environ("TEMP") & "\PPA_Map_" & Format(Now, "HHmmss") & ".html"

    Dim ado As Object
    Set ado = CreateObject("ADODB.Stream")
    ado.Type = 2: ado.Charset = "UTF-8": ado.Open
    ado.WriteText html
    ado.SaveToFile filePath, 2
    ado.Close: Set ado = Nothing

    Shell "explorer.exe """ & filePath & """", vbNormalFocus
    MsgBox "Map opened!  Geocoding " & locCount & " location(s) (~1 sec each).", _
           vbInformation, "CHFT Map ver 8.0"

End Sub


'=================================================================
'  HELPERS
'=================================================================
Private Function JSEsc(s As String) As String
    s = Replace(s, "\", "\\")
    s = Replace(s, "'", "\'")
    s = Replace(s, Chr(13), "")
    s = Replace(s, Chr(10), "")
    JSEsc = s
End Function

Private Function FormatPSF(p As String) As String
    Dim v As Double
    On Error GoTo FallbackPSF
    v = CDbl(p)
    If v <= 0 Then FormatPSF = "": Exit Function
    FormatPSF = "HK$" & Format(v, "#,##0") & "/sqft"
    Exit Function
FallbackPSF:
    FormatPSF = IIf(Trim(p) = "", "", p)
End Function

Private Function FormatPrice(p As String) As String
    Dim v As Double
    On Error GoTo Fallback
    v = CDbl(p)
    If v = 0 Then
        FormatPrice = "N/A"
    ElseIf v >= 1000000000 Then
        FormatPrice = "HK$" & Format(v / 1000000000, "0.00") & "B"
    ElseIf v >= 1000000 Then
        FormatPrice = "HK$" & Format(v / 1000000, "0.00") & "M"
    ElseIf v >= 1000 Then
        FormatPrice = "HK$" & Format(v / 1000, "0.0") & "K"
    Else
        FormatPrice = "HK$" & Format(v, "0") & "mil"
    End If
    Exit Function
Fallback:
    FormatPrice = IIf(Trim(p) = "", "N/A", p)
End Function


'=================================================================
'  HTML BUILDER
'=================================================================
Private Function BuildHTML(jsLocs As String, locCount As Long) As String

    Dim h As String

    h = "<!DOCTYPE html><html lang='en'><head>" & vbNewLine
    h = h & "<meta charset='UTF-8'>" & vbNewLine
    h = h & "<meta name='viewport' content='width=device-width,initial-scale=1'>" & vbNewLine
    h = h & "<title>CHFT Map ver 8.0</title>" & vbNewLine
    h = h & "<link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'/>" & vbNewLine

    '---- CSS ---------------------------------------------------------
    h = h & "<style>" & vbNewLine
    h = h & "* { margin:0; padding:0; box-sizing:border-box; }" & vbNewLine
    h = h & "html { overflow-y:auto; }" & vbNewLine
    h = h & "body { font-family:Arial,sans-serif; display:flex; flex-direction:column; min-height:100vh; }" & vbNewLine
    h = h & "#header { background:#1a237e; color:#fff; padding:10px 16px;" & vbNewLine
    h = h & "  display:flex; align-items:center; justify-content:space-between; flex-shrink:0; }" & vbNewLine
    h = h & "#header .title { font-size:16px; font-weight:bold; }" & vbNewLine
    h = h & "#header .stats { font-size:12px; opacity:.85; }" & vbNewLine
    h = h & "#container { display:flex; height:calc(100vh - 40px); overflow:hidden; }" & vbNewLine
    h = h & "#map { flex:1; }" & vbNewLine
    h = h & "#sidebar { width:340px; background:#fff; border-left:1px solid #ddd;" & vbNewLine
    h = h & "  display:flex; flex-direction:column; overflow:hidden; }" & vbNewLine

    ' Sidebar header
    h = h & "#sb-hdr { background:#283593; color:#fff; padding:10px 14px;" & vbNewLine
    h = h & "  font-size:13px; font-weight:bold; flex-shrink:0; }" & vbNewLine

    ' Controls bar
    h = h & "#sb-controls { background:#e8eaf6; border-bottom:1px solid #c5cae9;" & vbNewLine
    h = h & "  padding:7px 10px; flex-shrink:0; }" & vbNewLine
    h = h & "#sb-sort-row { display:flex; align-items:center; gap:6px; margin-bottom:5px; }" & vbNewLine
    h = h & "#sb-check-row { display:flex; align-items:center; gap:5px; }" & vbNewLine
    h = h & ".ctl-lbl { font-size:11px; color:#555; white-space:nowrap; }" & vbNewLine
    h = h & "#sb-sort { font-size:11px; padding:2px 4px; border:1px solid #bbb;" & vbNewLine
    h = h & "  border-radius:3px; flex:1; cursor:pointer; }" & vbNewLine
    h = h & ".sb-btn { font-size:11px; padding:2px 8px; border:1px solid #9e9e9e;" & vbNewLine
    h = h & "  border-radius:3px; background:#fff; cursor:pointer; }" & vbNewLine
    h = h & ".sb-btn:hover { background:#c5cae9; }" & vbNewLine
    h = h & "#sb-showing { font-size:11px; color:#888; margin-left:auto; white-space:nowrap; }" & vbNewLine

    ' List area
    h = h & "#loc-list { overflow-y:auto; flex:1; }" & vbNewLine

    ' Row item - flex to hold checkbox + content
    h = h & ".loc-item { padding:10px 12px; border-bottom:1px solid #eee; position:relative; }" & vbNewLine
    h = h & ".fix-btn{position:absolute;top:5px;right:5px;font-size:9px;padding:2px 6px;border-radius:3px;background:#6a1b9a;color:#fff;font-weight:600;cursor:pointer;border:none;line-height:1.5;white-space:nowrap;z-index:1;}" & vbNewLine
    h = h & ".fix-btn.active{background:#2e7d32;}" & vbNewLine
    h = h & "  display:flex; align-items:flex-start; gap:6px; transition:background .15s; }" & vbNewLine
    h = h & ".loc-item:hover  { background:#e8eaf6; }" & vbNewLine
    h = h & ".loc-item.active { background:#c5cae9; }" & vbNewLine
    h = h & ".loc-item.not-found { background:#fff3e0; }" & vbNewLine
    h = h & ".loc-item.dimmed { opacity:0.35; }" & vbNewLine

    ' Checkbox
    h = h & ".loc-cb { margin-top:4px; cursor:pointer; width:14px; height:14px; flex-shrink:0; }" & vbNewLine
    h = h & ".loc-extbtns{margin-top:4px;display:none;flex-basis:100%;}" & vbNewLine
    h = h & ".ext-btn{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;text-decoration:none;font-weight:600;margin-right:4px;}" & vbNewLine
    h = h & ".ext-btn.gm{background:#4285f4;color:#fff;}" & vbNewLine
    h = h & ".ext-btn.sv{background:#ff6d00;color:#fff;}" & vbNewLine
    h = h & ".sv-lbl{font-size:9px;color:#777;margin-right:2px;vertical-align:middle;}" & vbNewLine
    h = h & ".ext-btn.sv-auto{background:#9e9e9e;color:#fff;font-size:10px;padding:2px 6px;" & vbNewLine
    h = h & "  border-radius:3px;text-decoration:none;font-weight:700;margin-left:3px;}" & vbNewLine
    h = h & ".ext-btn.sv-auto.ready{background:#2e7d32;}" & vbNewLine
    h = h & ".ext-btn.sv-auto.failed{background:#b71c1c;}" & vbNewLine
    h = h & ".ext-btn.sv{padding:2px 5px;font-size:10px;font-weight:700;}" & vbNewLine
    h = h & ".map-nav-ctrl{display:flex;flex-direction:column;gap:4px;}" & vbNewLine
    h = h & ".map-nav-btn{display:block;background:#fff;border:2px solid rgba(0,0,0,.2);border-radius:4px;padding:5px 10px;font-size:11px;font-weight:700;text-decoration:none;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.2);}" & vbNewLine
    h = h & ".map-nav-btn.gm{color:#4285f4;border-color:#4285f4;}" & vbNewLine
    h = h & ".map-nav-btn.sv{color:#ff6d00;border-color:#ff6d00;}" & vbNewLine

    ' Content wrapper (takes remaining space, click = navigate)
    h = h & ".loc-content { display:flex; flex-wrap:wrap; gap:0; align-items:flex-start; flex:1; cursor:pointer; min-width:0; }" & vbNewLine
    h = h & ".loc-content > .badge,.loc-content > .badge.subj { flex-shrink:0; margin-right:8px; margin-top:2px; }" & vbNewLine
    h = h & ".loc-text { flex:1; min-width:0; }" & vbNewLine

    ' Badge / number circle
    h = h & ".badge { background:#1a237e; color:#fff; border-radius:50%;" & vbNewLine
    h = h & "  width:26px; height:26px; flex-shrink:0;" & vbNewLine
    h = h & "  display:flex; align-items:center;" & vbNewLine
    h = h & "  justify-content:center; font-size:11px; font-weight:bold; }" & vbNewLine
    h = h & ".badge.subj { background:#c2185b; }" & vbNewLine
    h = h & ".loc-item.not-found .badge { background:#e65100; }" & vbNewLine

    ' Text content
    h = h & ".sb-name  { font-weight:bold; font-size:13px; color:#1a237e; }" & vbNewLine
    h = h & ".sb-addr  { font-size:11px; color:#666; margin-top:2px; }" & vbNewLine
    h = h & ".sb-count { font-size:11px; color:#888; margin-top:3px; }" & vbNewLine
    h = h & ".sb-rows  { color:#bbb; font-style:italic; font-size:10px; }" & vbNewLine
    h = h & ".sb-price { font-size:11px; color:#c62828; font-weight:bold; margin-top:2px; }" & vbNewLine
    h = h & ".sb-psf { font-size:10px; color:#1565c0; font-weight:600; }" & vbNewLine
    h = h & ".sb-consid-row { font-size:10px; color:#555; margin-top:2px; }" & vbNewLine
    h = h & ".sb-detail { font-size:11px; color:#444; margin-top:5px; padding-top:5px;" & vbNewLine
    h = h & "  border-top:1px solid #ddd; line-height:1.8; }" & vbNewLine
    h = h & ".sb-dist { font-size:10px; color:#5c6bc0; margin-top:2px; font-style:italic; }" & vbNewLine
    h = h & ".nf-msg { font-size:11px; color:#e65100; font-style:italic; margin-top:3px; }" & vbNewLine
    ' Valuation section CSS
    h = h & "/* ©¤©¤ Valuation ©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤©¤ */" & vbNewLine
    h = h & "#val-section { padding:14px 20px 24px; background:#fafafa; border-top:3px solid #3f51b5; }" & vbNewLine
    h = h & "#val-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }" & vbNewLine
    h = h & ".val-title { font-size:15px; font-weight:bold; color:#3f51b5; }" & vbNewLine
    h = h & "#val-summary { font-size:12px; color:#555; }" & vbNewLine
    h = h & "#val-count { color:#3f51b5; font-weight:bold; }" & vbNewLine
    h = h & "#val-result { color:#b71c1c; font-size:13px; font-weight:bold; }" & vbNewLine
    h = h & ".val-toggle { cursor:pointer; background:none; border:1px solid #3f51b5; color:#3f51b5;" & vbNewLine
    h = h & "  padding:3px 9px; border-radius:3px; font-size:11px; margin-left:8px; }" & vbNewLine
    h = h & "#val-scroll { overflow-x:auto; margin-top:6px; }" & vbNewLine
    h = h & "#val-table { border-collapse:collapse; font-size:11px; white-space:nowrap; }" & vbNewLine
    h = h & "#val-table th { background:#3f51b5; color:#fff; padding:5px 7px; text-align:center;" & vbNewLine
    h = h & "  position:sticky; top:0; z-index:2; }" & vbNewLine
    h = h & "#val-table td { padding:4px 7px; border-bottom:1px solid #eee; vertical-align:middle; }" & vbNewLine
    h = h & "#val-table tr.adopted td { background:#e8f5e9; }" & vbNewLine
    h = h & "#val-table tr:not(.adopted):hover td { background:#f5f5f5; }" & vbNewLine
    h = h & ".val-input { width:56px; padding:2px 4px; border:1px solid #ccc; border-radius:3px;" & vbNewLine
    h = h & "  font-size:11px; text-align:right; box-sizing:border-box; }" & vbNewLine
    h = h & ".area-in { width:76px; } .weight-in { width:52px; }" & vbNewLine
    h = h & ".area-in.modified { border-color:#e53935; color:#e53935; background:#fff5f5; font-weight:bold; }" & vbNewLine
    h = h & ".area-in.modified:focus { outline-color:#e53935; }" & vbNewLine
    h = h & ".val-input:disabled { background:#f0f0f0; color:#aaa; border-color:#e0e0e0; }" & vbNewLine
    h = h & ".val-ur,.val-adjur { font-weight:bold; color:#1565c0; text-align:right; }" & vbNewLine
    h = h & ".val-netadj { text-align:right; color:#555; }" & vbNewLine
    h = h & ".val-adopt { text-align:center; }" & vbNewLine
    h = h & ".val-consid { text-align:right; }" & vbNewLine
    h = h & ".val-num { color:#3f51b5; font-weight:bold; text-align:center; }" & vbNewLine
    h = h & "#val-tfoot td { background:#fff3e0; font-weight:bold; border-top:2px solid #e65100; }" & vbNewLine
    h = h & "#val-subj-row td { background:#fce4ec; font-weight:bold; border-top:2px solid #ad1457; color:#880e4f; font-size:11px; padding:4px 7px; }" & vbNewLine
    h = h & ".subj-label { color:#ad1457; font-size:12px; font-weight:bold; }" & vbNewLine
    h = h & ".adj-td { white-space:nowrap; }" & vbNewLine
    h = h & ".pct-lbl { font-size:10px; color:#666; margin-left:1px; }" & vbNewLine
    h = h & ".adj-in.auto-calc { color:#1565c0; }" & vbNewLine
    h = h & ".val-hint { font-size:11px; color:#888; margin:0 0 6px; }" & vbNewLine
    h = h & ".mark-th { background:#e65100 !important; }" & vbNewLine
    h = h & "#val-s2-row{background:#fff3e0;}" & vbNewLine
    h = h & "#val-s2-row td{border-top:1px dashed #e65100;}" & vbNewLine
    h = h & ".s2-label{background:#e65100!important;color:#fff;font-weight:700;text-align:center;}" & vbNewLine
    h = h & ".add-s2-btn{cursor:pointer;background:#e65100;color:#fff;border:none;border-radius:3px;" & vbNewLine
    h = h & "  padding:1px 5px;font-size:11px;line-height:1.4;margin-left:3px;vertical-align:middle;}" & vbNewLine
    h = h & "#val-warn-ov{display:none;position:fixed;top:0;left:0;width:100%;height:100%;" & vbNewLine
    h = h & "  background:rgba(0,0,0,.45);z-index:9999;align-items:center;justify-content:center;}" & vbNewLine
    h = h & "#val-warn-box{background:#fff;border:2px solid #f44336;border-radius:8px;" & vbNewLine
    h = h & "  padding:20px 24px;max-width:280px;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.3);}" & vbNewLine
    h = h & ".adj-th  { background:#2e7d32 !important; }" & vbNewLine
    h = h & ".mark-sel { width:80px; font-size:10px; padding:1px 2px; border:1px solid #ff8a65; border-radius:3px; background:#fff3e0; cursor:pointer; }" & vbNewLine
    h = h & ".coeff-in::-webkit-inner-spin-button,.coeff-in::-webkit-outer-spin-button { opacity:1; }" & vbNewLine
    h = h & "#val-table th.sortable { cursor:pointer; user-select:none; }" & vbNewLine
    h = h & "#val-table th.sortable:hover { background:#5c6bc0; }" & vbNewLine
    h = h & "#val-table th .sort-arr { font-size:9px; margin-left:3px; opacity:0.5; }" & vbNewLine
    h = h & "#val-table th.sort-asc .sort-arr::after  { content:'\25B2'; opacity:1; }" & vbNewLine
    h = h & "#val-table th.sort-desc .sort-arr::after { content:'\25BC'; opacity:1; }" & vbNewLine
    h = h & "#val-coeff-row td { background:#e8eaf6; font-size:10px; padding:3px 5px; }" & vbNewLine
    h = h & ".coeff-lbl { color:#5c6bc0; font-weight:bold; font-style:italic; }" & vbNewLine
    h = h & ".coeff-in { width:48px; padding:1px 3px; border:1px solid #9fa8da; border-radius:2px; font-size:10px; text-align:right; background:#fff; }" & vbNewLine
    h = h & ".coeff-unit { color:#888; font-size:9px; margin-left:1px; }" & vbNewLine


    ' Status bar
    h = h & "#status { font-size:12px; color:#555; padding:6px 14px;" & vbNewLine
    h = h & "  background:#fff8e1; border-top:1px solid #ffe082; flex-shrink:0; }" & vbNewLine

    ' Fit-all button
    h = h & "#btn-fit { position:absolute; top:60px; right:350px; z-index:1000;" & vbNewLine
    h = h & "  background:#fff; border:2px solid #9e9e9e; border-radius:4px;" & vbNewLine
    h = h & "  padding:5px 10px; font-size:12px; cursor:pointer;" & vbNewLine
    h = h & "  box-shadow:0 2px 5px rgba(0,0,0,.3); }" & vbNewLine
    h = h & "#btn-fit:hover { background:#e8eaf6; border-color:#1a237e; }" & vbNewLine

    ' Map marker icons
    h = h & ".num-icon { background:#1a237e; color:#fff; border-radius:50%; width:28px; height:28px;" & vbNewLine
    h = h & "  display:flex; align-items:center; justify-content:center;" & vbNewLine
    h = h & "  font-size:11px; font-weight:bold;" & vbNewLine
    h = h & "  border:2.5px solid #fff; box-shadow:0 2px 6px rgba(0,0,0,.5); }" & vbNewLine
    h = h & ".subj-icon { background:#c2185b; color:#fff; border-radius:50%; width:34px; height:34px;" & vbNewLine
    h = h & "  display:flex; align-items:center; justify-content:center;" & vbNewLine
    h = h & "  font-size:14px; font-weight:bold;" & vbNewLine
    h = h & "  border:3px solid #fff; box-shadow:0 2px 8px rgba(0,0,0,.6); }" & vbNewLine

    ' Popup styles
    h = h & ".pu-title { font-weight:bold; font-size:14px; color:#1a237e; margin-bottom:4px; }" & vbNewLine
    h = h & ".pu-title.subj-title { color:#c2185b; }" & vbNewLine
    h = h & ".pu-addr  { font-size:12px; color:#555; margin-bottom:3px; }" & vbNewLine
    h = h & ".pu-rows  { font-size:11px; color:#aaa; font-style:italic; margin-bottom:6px; }" & vbNewLine
    h = h & ".pu-route { font-size:11px; background:#e8f5e9; color:#2e7d32; padding:4px 7px;" & vbNewLine
    h = h & "  border-radius:4px; margin-bottom:6px; }" & vbNewLine
    h = h & ".pu-divider { border:none; border-top:1px solid #ddd; margin:6px 0; }" & vbNewLine
    h = h & ".txn-line { font-size:12px; color:#333; padding:4px 0;" & vbNewLine
    h = h & "  border-bottom:1px solid #f0f0f0; line-height:1.5; }" & vbNewLine
    h = h & ".txn-line:last-child { border-bottom:none; }" & vbNewLine
    h = h & ".txn-price { color:#c62828; font-weight:bold; }" & vbNewLine
    h = h & ".txn-label { color:#888; font-size:11px; }" & vbNewLine
    h = h & "</style></head><body>" & vbNewLine

    '---- HTML structure ---------------------------------------------
    h = h & "<div id='header'>" & vbNewLine
    h = h & "  <span class='title'>CHFT Map ver 8.0</span>" & vbNewLine
    h = h & "  <span class='stats'>" & locCount & " location(s)</span>" & vbNewLine
    h = h & "</div>" & vbNewLine
    h = h & "<div id='container'>" & vbNewLine
    h = h & "  <div id='map'></div>" & vbNewLine
    h = h & "  <button id='btn-fit'>Fit All</button>" & vbNewLine
    h = h & "  <div id='sidebar'>" & vbNewLine
    h = h & "    <div id='sb-hdr'>Locations (" & locCount & ")</div>" & vbNewLine
    h = h & "    <div id='sb-controls'>" & vbNewLine
    h = h & "      <div id='sb-sort-row'>" & vbNewLine
    h = h & "        <span class='ctl-lbl'>Sort:</span>" & vbNewLine
    h = h & "        <select id='sb-sort'>" & vbNewLine
    h = h & "          <option value='no'>Pin No.</option>" & vbNewLine
    h = h & "          <option value='name'>Name A to Z</option>" & vbNewLine
    h = h & "          <option value='price'>Price (high to low)</option>" & vbNewLine
    h = h & "          <option value='count'>Transactions (most first)</option>" & vbNewLine
    h = h & "          <option value='dist'>Distance to Subject</option>" & vbNewLine
    h = h & "          <option value='time'>Travel Time to Subject</option>" & vbNewLine
    h = h & "        </select>" & vbNewLine
    h = h & "      </div>" & vbNewLine
    h = h & "      <div id='sb-route-row' style='display:flex;align-items:center;gap:6px;margin-bottom:5px;'>" & vbNewLine
    h = h & "        <span class='ctl-lbl'>Route:</span>" & vbNewLine
    h = h & "        <label style='font-size:11px;cursor:pointer;'>" & vbNewLine
    h = h & "          <input type='radio' name='rmode' value='driving'> Car" & vbNewLine
    h = h & "        </label>" & vbNewLine
    h = h & "        <label style='font-size:11px;cursor:pointer;'>" & vbNewLine
    h = h & "          <input type='radio' name='rmode' value='foot' checked> Walk" & vbNewLine
    h = h & "        </label>" & vbNewLine
    h = h & "        <span id='route-mode-lbl' style='font-size:10px;color:#888;margin-left:auto;'>walking mode</span>" & vbNewLine
    h = h & "      </div>" & vbNewLine
    h = h & "      <div id='sb-check-row'>" & vbNewLine
    h = h & "        <button class='sb-btn' id='btn-all'>All</button>" & vbNewLine
    h = h & "        <button class='sb-btn' id='btn-none'>None</button>" & vbNewLine
    h = h & "        <span id='sb-showing'></span>" & vbNewLine
    h = h & "      </div>" & vbNewLine
    h = h & "    </div>" & vbNewLine
    h = h & "    <div id='loc-list'></div>" & vbNewLine
    h = h & "  </div>" & vbNewLine
    h = h & "</div>" & vbNewLine
    h = h & "<div id='status'>Geocoding, please wait...</div>" & vbNewLine
    ' Valuation HTML
    h = h & "<div id='val-section'>" & vbNewLine
    h = h & "  <div id='val-hdr'>" & vbNewLine
    h = h & "    <div style='display:flex;align-items:center;'>" & vbNewLine
    h = h & "      <span class='val-title'>Valuation Analysis</span>" & vbNewLine
    h = h & "      <button class='val-toggle' onclick='toggleVal()'>Hide</button>" & vbNewLine
    h = h & "    </div>" & vbNewLine
    h = h & "    <div id='val-summary'>" & vbNewLine
    h = h & "      Adopted: <span id='val-count'>0</span> comparables &nbsp;|&nbsp;" & vbNewLine
    h = h & "      Adopted Unit Rate: <span id='val-result'>&#8212;</span> /sq ft" & vbNewLine
    h = h & "    </div>" & vbNewLine
    h = h & "  </div>" & vbNewLine
    h = h & "  <div id='val-body-wrap'>" & vbNewLine
    h = h & "    <p class='val-hint'>Check <b>Adopt</b> to include a comparable. " & vbNewLine
    h = h & "      Fill in <b>Saleable Area</b> (sq ft) to calculate unit rates. " & vbNewLine
    h = h & "      Adjustment factors in % (positive = upward, negative = downward). " & vbNewLine
    h = h & "      Weights are auto-equalised when you adopt a comparable; adjust as needed.</p>" & vbNewLine
    h = h & "    <div id='val-scroll'>" & vbNewLine
    h = h & "      <table id='val-table'>" & vbNewLine
    h = h & "        <thead>" & vbNewLine
    h = h & "        <tr id='val-head-1'>" & vbNewLine
    h = h & "          <th class='sortable' data-col='0'>Adopt<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='1'>#<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='2'>Building<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='3'>Date<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='4'>Nature<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='5'>Usage<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='6'>OP<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='7'>CP/PT<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='8'>Unit<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='9'>Flr<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='10'>Blk<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='11'>Phase<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='12'>Consid-<br>eration<br>(HK$)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='13'>Saleable Area<br>(sq ft)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='14'>Unit Rate<br>(HK$/sq ft)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable mark-th' data-col='15'>Location<br>Mark<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable mark-th' data-col='16'>View<br>Mark<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='adj-th'>Time<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>Location<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>Floor<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>Bldg Age<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>Size<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>View<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='adj-th'>Others<br>Adj (%)</th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='24'>Net<br>Adj (%)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='25'>Adj. Unit Rate<br>(HK$/sq ft)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "          <th class='sortable' data-col='26'>Weight (%)<span class='sort-arr'></span></th>" & vbNewLine
    h = h & "        </tr>" & vbNewLine
    h = h & "        <tr id='val-coeff-row'>" & vbNewLine
    h = h & "          <td colspan='15' class='coeff-lbl' style='text-align:right;'>" & vbNewLine
    h = h & "            Default rate coefficients &#8594;</td>" & vbNewLine
    h = h & "          <td class='mark-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-loc-mark' type='number' value='1' step='1'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/mk</span></td>" & vbNewLine
    h = h & "          <td class='mark-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-view-mark' type='number' value='1' step='1'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/mk</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-time' type='number' value='0.5' step='0.1'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/mth</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-loc' type='number' step='0.5' value='0'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-flr' type='number' step='0.5' value='2.0'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/flr</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-age' type='number' step='0.5' value='0.5'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/yr</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-size' type='number' step='0.5' value='-0.5'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%/10ft&#178;</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-view' type='number' step='0.5' value='5.0'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-th' style='text-align:center;'>" & vbNewLine
    h = h & "            <input class='coeff-in' id='coeff-oth' type='number' step='0.5' value='0'>" & vbNewLine
    h = h & "            <span class='coeff-unit'>%</span></td>" & vbNewLine
    h = h & "          <td colspan='3'></td>" & vbNewLine
    h = h & "        </tr>" & vbNewLine
    h = h & "        </thead>" & vbNewLine
    h = h & "        <tbody id='val-tbody'></tbody>" & vbNewLine
    h = h & "        <tfoot>" & vbNewLine
    h = h & "        <tr id='val-subj-row'>" & vbNewLine
    h = h & "          <td class='subj-label' style='text-align:center;white-space:nowrap;'>S &nbsp;<button id='add-s2-btn' onclick='toggleS2()' class='add-s2-btn' title='Add S2'>+</button></td>" & vbNewLine
    h = h & "          <td id='subj-no-cell' class='val-num'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-name-cell' style='max-width:150px;overflow:hidden;text-overflow:ellipsis;'>&#8212;</td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "          <td id='subj-nature-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-usage-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-op-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td></td><td></td>" & vbNewLine
    h = h & "          <td id='subj-flr-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-blk-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-phase-cell'>&#8212;</td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "          <td id='subj-area-cell' class='val-ur'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='subj-ur-disp' class='val-ur' style='color:#880e4f;'>&#8212;</td>" & vbNewLine
    h = h & "          <td><select id='subj-loc-mark' class='val-input mark-sel'><option value=''>&#8211;</option><option value='5'>5 Good</option><option value='4'>4 Okay</option><option value='3' selected>3 Average</option><option value='2'>2 Below Avg</option><option value='1'>1 Poor</option></select></td>" & vbNewLine
    h = h & "          <td><select id='subj-view-mark' class='val-input mark-sel'><option value=''>&#8211;</option><option value='5'>5 Good</option><option value='4'>4 Okay</option><option value='3' selected>3 Average</option><option value='2'>2 Below Avg</option><option value='1'>1 Poor</option></select></td>" & vbNewLine
    h = h & "          <td colspan='7'></td>" & vbNewLine
    h = h & "          <td></td><td></td><td></td>" & vbNewLine
    h = h & "        </tr>" & vbNewLine
    h = h & "        <tr id='val-s2-row' style='display:none;'>" & vbNewLine
    h = h & "          <td class='s2-label' style='white-space:nowrap;'>" & vbNewLine
    h = h & "            S2 <button onclick='toggleS2()' style='cursor:pointer;background:#b71c1c;" & vbNewLine
    h = h & "              color:#fff;border:none;border-radius:3px;padding:1px 4px;font-size:10px;'>&#10005;</button></td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "          <td style='font-style:italic;font-size:10px;color:#666;'>Adj from S &#8594;</td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "          <td></td><td></td>" & vbNewLine
    h = h & "          <td><input id='s2-op-inp' type='text' class='val-input' placeholder='YYYY/MM' style='width:62px;'></td>" & vbNewLine
    h = h & "          <td></td><td></td>" & vbNewLine
    h = h & "          <td><input id='s2-flr-inp' type='number' class='val-input' step='1' placeholder='Flr' style='width:44px;'></td>" & vbNewLine
    h = h & "          <td></td><td></td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "          <td><input id='s2-area-inp' type='number' class='val-input' step='1' placeholder='SA' style='width:52px;'></td>" & vbNewLine
    h = h & "          <td id='s2-base-ur' class='val-ur' style='color:#880e4f;font-size:10px;'>&#8212;</td>" & vbNewLine
    h = h & "          <td><select id='s2-loc-mark' class='val-input mark-sel'><option value=''>&#8211;</option><option value='5'>5 Good</option><option value='4'>4 Okay</option><option value='3' selected>3 Average</option><option value='2'>2 Below Avg</option><option value='1'>1 Poor</option></select></td>" & vbNewLine
    h = h & "          <td><select id='s2-view-mark' class='val-input mark-sel'><option value=''>&#8211;</option><option value='5'>5 Good</option><option value='4'>4 Okay</option><option value='3' selected>3 Average</option><option value='2'>2 Below Avg</option><option value='1'>1 Poor</option></select></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-time-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-loc-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-flr-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-age-adj' class='val-input s2-adj-in' step='0.5' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-sz-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-view-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td class='adj-td'><input type='number' id='s2-oth-adj' class='val-input s2-adj-in' step='0.1' value='0'><span class='pct-lbl'>%</span></td>" & vbNewLine
    h = h & "          <td id='s2-netadj' class='val-netadj'>&#8212;</td>" & vbNewLine
    h = h & "          <td id='s2-ur-disp' class='val-adjur' style='color:#b71c1c;font-weight:700;'>&#8212;</td>" & vbNewLine
    h = h & "          <td></td>" & vbNewLine
    h = h & "        </tr>" & vbNewLine
    h = h & "        <tr id='val-tfoot'>" & vbNewLine
    h = h & "          <td colspan='26' style='text-align:right;'>Weighted Average Adopted Unit Rate:</td>" & vbNewLine
    h = h & "          <td id='val-wavg' style='text-align:right;color:#b71c1c;'>&#8212;</td>" & vbNewLine
    h = h & "        </tr>" & vbNewLine
    h = h & "        </tfoot>" & vbNewLine
    h = h & "      </table>" & vbNewLine
    h = h & "    </div>" & vbNewLine
    h = h & "  </div>" & vbNewLine
    h = h & "</div>" & vbNewLine


    '---- JavaScript --------------------------------------------------
    h = h & "<script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script>" & vbNewLine
    h = h & "<script>" & vbNewLine
    h = h & jsLocs & vbNewLine & vbNewLine

    ' Map init  -- CHFT v8.0 tile provider: Esri ArcGIS hosted basemaps
    ' (keyless, no strict Referer policy). Auto-fallback to next provider
    ' on tileerror so the map still renders if one service hiccups.
    h = h & "var map = L.map('map').setView([22.3193,114.1694],13);" & vbNewLine
    h = h & "var __tileProviders=[" & vbNewLine
    h = h & "  {url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',sub:'',attr:'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community',max:18}," & vbNewLine
    h = h & "  {url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',sub:'',attr:'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community',max:18}," & vbNewLine
    h = h & "  {url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',sub:'',attr:'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community',max:18}" & vbNewLine
    h = h & "];" & vbNewLine
    h = h & "var __tIdx=0,__tiles;" & vbNewLine
    h = h & "function __mkTiles(){" & vbNewLine
    h = h & "  var p=__tileProviders[__tIdx];" & vbNewLine
    h = h & "  var tl=L.tileLayer(p.url,{attribution:p.attr,maxZoom:p.max,subdomains:p.sub});" & vbNewLine
    h = h & "  tl.on('tileerror',function(){" & vbNewLine
    h = h & "    if(!tl.__errored){tl.__errored=true;setTimeout(function(){" & vbNewLine
    h = h & "      if(__tIdx<__tileProviders.length-1){__tIdx++;map.removeLayer(tl);__tiles=__mkTiles();__tiles.addTo(map);}" & vbNewLine
    h = h & "    },1500);}" & vbNewLine
    h = h & "  });" & vbNewLine
    h = h & "  return tl;}" & vbNewLine
    h = h & "__tiles=__mkTiles();__tiles.addTo(map);" & vbNewLine & vbNewLine

    ' Geocoder helper  -- CHFT v8.0: Nominatim first, Photon as fallback.
    ' Photon (https://photon.komoot.io) is also OSM-based, keyless, and
    ' does not enforce the strict referer / User-Agent policy Nominatim
    ' now applies. Photon's response is GeoJSON, so coordinates are
    ' [lng, lat] (opposite order from Nominatim's [lat, lon]).
    h = h & "function __geocodePhoton(q,cb){" & vbNewLine
    h = h & "  fetch('https://photon.komoot.io/api/?q='+encodeURIComponent(q)+'&limit=1'," & vbNewLine
    h = h & "    {headers:{'Accept-Language':'en'}})" & vbNewLine
    h = h & "    .then(function(r){return r.json();})" & vbNewLine
    h = h & "    .then(function(d){" & vbNewLine
    h = h & "      if(d&&d.features&&d.features[0]&&d.features[0].geometry&&d.features[0].geometry.coordinates){" & vbNewLine
    h = h & "        var c=d.features[0].geometry.coordinates;" & vbNewLine
    h = h & "        cb({lat:parseFloat(c[1]),lng:parseFloat(c[0]),src:'photon'});" & vbNewLine
    h = h & "      }else{cb(null);}})" & vbNewLine
    h = h & "    .catch(function(){cb(null);});}" & vbNewLine
    h = h & "function __geocode(q,cb){" & vbNewLine
    h = h & "  fetch('https://nominatim.openstreetmap.org/search?format=json&limit=1&q='+encodeURIComponent(q)," & vbNewLine
    h = h & "    {headers:{'Accept-Language':'en'}})" & vbNewLine
    h = h & "    .then(function(r){return r.json();})" & vbNewLine
    h = h & "    .then(function(d){" & vbNewLine
    h = h & "      if(d&&d.length&&d[0].lat&&d[0].lon){" & vbNewLine
    h = h & "        cb({lat:parseFloat(d[0].lat),lng:parseFloat(d[0].lon),src:'nominatim'});" & vbNewLine
    h = h & "      }else{__geocodePhoton(q,cb);}})" & vbNewLine
    h = h & "    .catch(function(){__geocodePhoton(q,cb);});}" & vbNewLine & vbNewLine

    ' Map nav buttons (bottom-right Leaflet control)
    h = h & "var MapNav=L.Control.extend({" & vbNewLine
    h = h & "  options:{position:'bottomright'}," & vbNewLine
    h = h & "  onAdd:function(){" & vbNewLine
    h = h & "    var d=L.DomUtil.create('div','map-nav-ctrl');" & vbNewLine
    h = h & "    d.innerHTML='<a id=""map-gm-btn"" class=""map-nav-btn gm"" href=""#"" target=""_blank"">&#128205; Google Maps</a>'" & vbNewLine
    h = h & "      +'<a id=""map-sv-btn"" class=""map-nav-btn sv"" href=""#"" target=""_blank"">&#128247; Street View</a>';" & vbNewLine
    h = h & "    L.DomEvent.disableClickPropagation(d); return d;}});" & vbNewLine
    h = h & "new MapNav().addTo(map);" & vbNewLine
    h = h & "var DragCtrl=L.Control.extend({options:{position:'bottomright'}," & vbNewLine
    h = h & "  onAdd:function(){var d=L.DomUtil.create('div');" & vbNewLine
    h = h & "    var b=L.DomUtil.create('button','drag-mode-btn',d);" & vbNewLine
    h = h & "    b.id='drag-mode-btn';b.textContent='\uD83D\uDD13 Move Pins';" & vbNewLine
    h = h & "    b.onclick=toggleDragMode;" & vbNewLine
    h = h & "    L.DomEvent.disableClickPropagation(d);return d;}});" & vbNewLine
    h = h & "new DragCtrl().addTo(map);" & vbNewLine & vbNewLine
    h = h & "function bearingTo(lat1,lng1,lat2,lng2){" & vbNewLine
    h = h & "  var dL=(lng2-lng1)*Math.PI/180;" & vbNewLine
    h = h & "  var r1=lat1*Math.PI/180,r2=lat2*Math.PI/180;" & vbNewLine
    h = h & "  var y=Math.sin(dL)*Math.cos(r2);" & vbNewLine
    h = h & "  var x=Math.cos(r1)*Math.sin(r2)-Math.sin(r1)*Math.cos(r2)*Math.cos(dL);" & vbNewLine
    h = h & "  return (Math.atan2(y,x)*180/Math.PI+360)%360;}" & vbNewLine & vbNewLine

    h = h & "function svLink(lat,lng,dir){" & vbNewLine
    h = h & "  var D=0.00045,oLat=lat,oLng=lng;" & vbNewLine
    h = h & "  if(dir==='N') oLat=lat+D;" & vbNewLine
    h = h & "  else if(dir==='S') oLat=lat-D;" & vbNewLine
    h = h & "  else if(dir==='E') oLng=lng+D;" & vbNewLine
    h = h & "  else oLng=lng-D;" & vbNewLine
    h = h & "  var hdg=Math.round(be            t=lllll  vbbbbb(r2  b(r2  b(r2  b(r2               &      &      &      &      &                                                 0/MMMMMathhhhh ¢¢¢¢¢£  ¤                                                                                             ir))))©  ª «  ¬       2=lat2*Math.PI/180;" & vbNewLine
    h = h & "  var y=Math.sin(dL)*Math.cos(r2);" & vbNewLine
    h = h & "  var x=Math.cos(r1)*M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯¯¯¯¯ M¯ ±  ² ³  ¼                                                                 ineeeee       ineeeee       ineeeee ¹  º »  ®        »  ®        »  ®        »            »            »              ÀÀÀÀÀÁ  Â      lng  ÅÅÅÅÅlattttÇ  È É  Ê       & Í    & Í   Ï  Ð Ñ  Ò       &  ag-ÿcreate('button','drag-mode-btn',d)ag-mode-btn',d)ag-mode-bt ×  Ø Ù  Ú      ionÚ      ionÚ      ionÚ latttttß  à       â rinnnnnariiiiå  æ ç  è       êêêêê  è       êêêêêêêè       êêêêêê í  î ï  ð       ò  ð       ò  ð       ò  ð       ò õ  ö ÷  ø       úúúúúh ===== h     ý  þ              r2)   ))))©  ª «³  ¼    -mode-bt ×  Ø Ù  ionÚ      ion  ²¶ ionÚ  zr  zr  zrPI/180;" & vbNewLine
 ine
    h = h & "  v 'h tr2);" & vbNewLine
:   â rinnnnnariii& vbNewLine
    h = h & "ar .¯ M¯¯¯¯¯ M¯ ±  ²                  .c                                  ina s/@          »              ÀÀÀÀÀÁ »  ®        »         ion  ²¶ ionÚ  z1    Ï  ¼t=                   an           Úiiå&
    h = = h & "phdg& "  else if(dir=êêêêêêè       êir=êêêêêêè   '  '  '  '  '  '   vbNewLF        &d(mod)ò  ð     tomright'}," & gnew Ma            & e  te(À É  É  p= h & "  varch=5'ion bn   estOnSeg(p  inpfuncavarca=5'ibn bnb  ep);"dx=  vb- if(ddy(p atnpf='dr2=dx*dx+dy*dy    ir))))©  ªar l2 vb0)        {± : rinlon ªang} '  '  '  '   v  elt=(   ng    )    ð        nloy)/l2  ð     tomrigt   Ïmax(0, & min(1,tg-mode-btn',d)ag-mde-btnwLF    +t an,       -btnx}      ion  ²  he
    h = h & "b180SVDir(ways,r2); & vbNewLine
:       !n, ||     .length)tttß  à               t=l:'S',v" &:2  - = h & "vL     n,ttß:0,     ion  ² innas|| 'No roads fy=Ma ê  èby \u2013 defaulton' to S, try manually'NewLine
:   â rir    D=Infinity,proj=null,|   Waynfini .¯ M¯¯¯¯¯ M¯ ±bn  .forEach(*Math.co(w×  Ø Ù  Ú       tom.geome   ||         tmright'<2êêêêê í= h & "vL     n ªanr( òi=0;i<null,|   Waynfini-1;i++     ion  ² i & "  a=       to[i],b  ² i & " +1] to S, try manualight'<p=    ¯ M¯¯¯¯¯ Line
 ,a.¯ M¯e
on,b¯ Lin
oon)     n ªanr( òi=earind à   hypot(pi],b-    on-Ú  ,b  ² i & " +1]loy)d< gn){'}," d;     p;(*Ma Éwè       êêêêêê  h = h & "    L.  on','drag-mode  h = h & "    L.} Line
 ,a.¯ M¯e
on','drag-moity,pr    M¯ ±bn   {  +t an,       -b    ion    ion  ²  h       &   h = h & "    L. ,r2); & vRLin data,r2/180ed no coor   ates ||     .length)tttß     t=l:'S',v" &:2on'nlon ªang}  ² b=  Ø Ù     on      ¯ M      ¯¯ Line
 ,a.¯ M¯etton','drag-mo  ²   +=b>=315||b<45?iå:b<135?iE':b<225?iS':'W'b-    on-Ú  ,b nlon ªang}  ²  =  Ø Ù        ¯ M      ¯¯  ng    b-    on-Ú  ,b nlon ªang}  ² rn & "  É&&(*Ma É.tags&&(*Ma É.tags.name?(*Ma É.tags.name:'unnamedewLin'b-    on-Ú  ,on','drag-moit  todmanr( òe-bt ×'},"*111   )b-    on-Ú  ,b nlon ªang} nwLF    +t  +,          ¯ M    i      p=   ²  he
    h = h & nlon ªang} s,r2); & vbionewLin: '+rn+' ('+dm+'m '+  ++' of buil    )     Ò       &  ag-     & 'button','drag-mode-btn',dupdMapNav(<2êêêêê í= h & "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "v "@ "vtCe  "v "v "v "v "v "v "v "v "v "v"v "v',l "v "v "v "vv ""v "v "v "v "v "vgv "v "v "v "doc "v "v "v "v v "v "v"v "v "v "v¡ "v "vI('v -"v "v "v "v "v "v "vt'v "v "v "v "v"v "v "v "v "v "v "v -"v "v "v "v "v "v "v "v§s2v "v "vne.=gm "v "v "v gm "v "v "v "ve"v "v "v "v=Ov "v "v "v "v "vv c/¤ "v "v "u "v "vv "v "v " lav "vlnI('v -"v "v "v "v "v "vv=sv) " "v  "v "vparseFloatv ),parseFloatvn "v "v "v "v "v "v "v "docon "voveend',vv ""v); vv ""v I('v -"v "vv "v "v "v "v  ' St"v"ve"v "v "v "vtmarkers=[v v "s=[vtotal=loca "vs"v "vI('v -"v "v "v "v "v " "vtchecked={},sor=g='"v, "vveI "v-1, "teLayer"v "vsubjectI "v-1, "teMode='foot "v "v "v "v v "v "v  "vtdistC "e={driv "v:{},foot:{}};  // per-mode c "ev "v "v "v "v"v "v "vloca "vs" "v "v "v "vloc,i "v "v "v "v v "v "v "vchecked "v=trueI('v -"v "v "v "v "v " "v loc.isSubject "v1) ubjectI "viI('v -"v "v "v "v "v ""v "v "v "v v "v "v "v "v  ' Ic"vf "vory"ve"v "v "v "v "vmakeIc"v(no,isSubj "v "v "v "v v "v "v "vv=isSubj "v "v L.divIc"v({classN "v:'' "v "v "v "v  "v Ç¶@h  "v """"h  "v "vl:"v "v "v "v"v "v"v ""v "v "cl "vs=""subj-icon"">S</"v>'," "vvbNewLine
v "v = v "v "v "vSize:[34,34],"v Anchor:[17,17],popup "vtdist0,-20]}); "v "v "v "return L. "vI "v({"v "Name:' "v "v "v "v "vnum "v "v'+no+' "v "v "v "v "28,28"v "v "v "v4,1 "v "v "v "v "16 "v}"v "v "v "v "v "v "v "v "' Parse price for sorting "v "v "v "function p"v"Pv "Val(s){ "v "v "v var psf=s.match(/[\$\u0024]([\d,]+)\/sqft/i"v "v "v¡ "vif(({")  "v"v v:''Float "v[1].replace(/,/g,'') "v "v "v "vv "vc"vsplit('\u2013'). "().trim(v "m=p "v "v([0-9. "v(mil|k|m|b) ubje!m('v -"v0 "v "v "v "v "vv="vloc,i "vm "v),u=m[2].toLowerCaseivIc"v({classN "v'v -"u==='b'?v*1e9:[2].tmLowerC6seivIckv({cla3:m('v6chor:[17,17], "vRoute draw{},foot:{}};  // pdocument.querySelectorAll('input[nv =rmode] "ubjEa"v v "v(r{},foot:{}};  //r.addEv{clListener('change',"vRoute daw{},foot:{}};    rc"vsMLow=this.valueine
v "v = v  v "v "vgetEle "vById('] "u-"m=p-lbl=p textCon -t= "v "v "v " "v(,17], "vRvtdfoot'?'walk "v ange':'drivvtdfoot'?,i "v "v "v "  [vthis.Laye[\d map"vmove "v daw{}'?,i ";'v -e "v=null;  "vtdist0,-20] "v "vne.=gbjEatCoagmveIdx>=0&&!locas2vs[0] "v "].isSv  "v&&markerjEatCoagmve/[\$\u0024]([\d,]+   "vll=t0,-20] "v "vne.vspLatLng" ""v "v "v(i "v "v,ll.latv "vngor:[17,17], "vRou('change',"vRoute f( "vBy "vdst0'||ange',"vRotime'vI(nderSidebarvtdistC "e={driv "v "v "v "nge',"vRotime'v "vhv "v """h  "v(mil|kBv Url(mAll('input[nv =oca "v(m||4,1 "v)ye[\ "v"v({"v "Name:'?'https://nv MLo.openstreet "u-de/nv ed-||4/nv e/v1/||4/'1 "v)ye[\ "v"v "v "vloca :'https://nv MLo.openstreet "u-de/nv "(d-car/nv e/v1/c"vsMLo/'aw{},foot:{}};  //  "v "vlod,]+   "vll=tvtdistC "e={driv  "v "vlo "nge',".triocas2vfromfoo,fromgmv,fromg"v& "v "v "v"v "v  "v "vlogetEle "vById('] "u-"m=p-lbl=p tex "(n -t= "v "v "v " "v(,17], "vRvv "v"v "v "vlogetEl  //[\dfoo<0||!"v "vn  //[\dfoo] "v "'aw{},foot:{}};  //  "v "vloge,-20s] "v "vn  //[\dfoo]CoagmveIdx>=1 "v)ye[\ "v"v "v "vloca,-20url=nv =me'v "v)+fromgng+','+fromgat+';'+s].lng+','+s].lat1 "v)ye[\ "v"v "v "vloca  +'?p-lrview=f "v&geomenues=geojson'aw{},foot:{}};  /ngev "v "v  fesubjurl"vhen( "nge',,i "All('inr.json7,1})1 "v)ye[\ "v"v "v "vloca  .then( "nge',,dhv "v """h  "v(ngev "v "v  ngeetEl!d.nv =s||!d.nv =s[0] "v "'aw{},foot "v "v"v "v "v "e={dt0,-20geo=d.nv =s[0]Coaomenyaw{},foot:{}};  //  "v "vlogengee,-20 "v=(d.nv =s[0]C "vance/1000"voFixed(1)+'km'aw{},foot:{}};  /ngev "v "v  ngee,-20ur=Math.nvnd(d.nv =s[0]Cur "v /60)+'min'aw{},foot:{}};  ngeev "v "v  ngeenv = "v LCoaoJSON(geo,{style:{coli "'#e91e63',weight:4,opacity:0.7}})({clTo( "u&&markerjEatCoagmngev "v "v  ngee "vC "ve[nv =7],][fromfoo]={ "v:function p"d.nv =s[0]C "vance/1000",,ll:d.nv =s[0]Cur "v /60}aw{},foot:{}};  //  "v "vlogengee,"clel=ySelectorAll('input[nv =rmode]info-'+fromfoo&&markerjEatCoagmngev "v "v  ngee,-20};  Label=nv =7],"v(m||4,?'Wfoo  rCar'aw{},foot:{}};  /ngev "v "v  ngee:'del) el. v "v(r{}'ocas2 to Laye[\d ('+};  Label+'): '+ "v+' / '+uraw{},foot:{}};  //  "v "vlogengeeupdateD "vLabel(fromfoo&&markerjEatCoagmngev "v "v  ng})( ""vv="vloc,){ "vBy "vdst0'||ange "v "vlod,]+   "vll=tvtdi"v "v "voot'?'' Bulk"vRoti  "vance calcu.is "oot'?'walk "v a "nge',"calcje!D "vances(};  ,donehv "v """h  "v(ngev "v "v  :'d  //[\dfoo<0||!"v "vn  //[ctfoo]){etEldonehrySn "v All('i(,17], "vRvtdfoonge "v "vlot0,-20s]=mÉµarkers[subjectIdx].getLatLng();" & vbNewLine
    h = hoot"  var modeLabel=Roti==='foot'?'Walk':'Drive'otitodo=locations.map(func //[(_,i){return i;})ou('change',"v  .filtere63',weighrkerjEatCo!enues=ge[i].isSoo,fro&&me/nv "(i]el="v "'aw{},footath.nvn next( ng "v LCoaoJSON(if(i>=1 ".length){ i;}done)  LC"v  "v)+; }loc,){ "vBy "v "vidxvBy h  ,lalu "v"v "v "v var ps"v "vurl=routeBaseUrl( "v)+ll.lng+','ps"vat+';'+sdxvBy h  ,eUrl(at "vll=tvtdi"v "  +'?overview=falsdfoo<0||!"v "vn e]icumentv UEleew=fById('status').t,-2Contrl(=;" & vbNewLine
  'Calcul]+ ng '+v "erSid+' div nces ('+(i+1)+'/'+w{},foot:{}+Co!..Rotime'vI(nderS  fetch( /6vbNhenv1/c"vsMLorhv "v r.json})1otime'v "vhv "vhvt ,eUrl(at "vld){s "oot'?'walk "v  v "v.ot:{}s&&dd){s "[0]tus').;" th.Cache[,fro][d (]={"(i]: /6vbNhenv1.isSoo,fr/1000,time).;" th.Cache[ur"v "/60})({clTo( "u&&ma"v &ma"updateDndefoot'(nv1,  fe"vloca  +'?p-lrv(]={"(00",,ll:d.nv =s"u&&setTot:,"i;})ou('ch){0]Curv ;},400s "[0]tus').;" th  }).s=gNewt:{}};  /){h.Cache[ur"v "/60)({cl}})({clTo( "u&&ma"vsMLorhv "v'walk&&dd){s "[0]tuh){0]Curv ;}C "e={driv v  "' Ug+',' sidebngeaw{},foo l',". for d.n item:{}};  //  "vot:{}};  By h  ,l "v """h  "v(oca o.ope||eet "u .filtere63',we,". ||dokerjEat,  f-lbl-w{}+'f-l  .id+' div nces (idebd=By lcje!D s(};  ; "v!el||!d)oca,-20u/ '+uraw{},fo"v(icon=mnderSidebarvtdistC "velvance/1000"vdoke+':v dv="v.toFixed(ngeekm /"vMath,foond(d.)( foomin LCoaoJSON(gedxvBy h  ,ehvt;  /ngevur"v "/60)({clAll:{}+Co!..s(tdi"v "  +'?overvhv;  /ngev  uraw{},foo).sE]ic"vld){s ""v ",  f-lbl-w{}+'f-l /ng /ngev "  0)({cl}})({clTo( , ;}C); 0)({cl}})({clTo( ,'ds2 ing';  By h  ,l "v "hv;  /ngev  .Cache[ur"v "/60}hv/ng /ngevin LCoaoJSON(gedx'aw{},foot){0]CurParse transa //  l,fo}).s=popup};  /){h.Cache[ur" //  parseTxna,-2(l,fotdi"v "  +'?overvhv;  /ngev  ncesf=l,fo.split('|';  By h  ,l "v hv/ng /ngevRvtdfocppt=f=tvrv-',unit=f=1]rv-',flr=(f[2]?f[2]th.F'oc-';  By h  ,l "v hv/ng /ngevRvtdf blk=f=3]rv-',phs=f=4]rv-' "e={driv v  " /ng /ngev "ncesnetpsf=f=12]&&f[12].trim()?f[12].trim():'eekm /"vMath,foonhv;  /ngev  nces(id00s=f=5]&&f[5].trim()?f[5].trim():'eekm /"vMath,fot:{}};  /ngev "vr prict:{}p=netpsf||(id00srvN/Aeekm /"vMath,foonhv;  /ngev  ncesn)oure=f=6]rv-',foot=f=7]rv-' "e={driv foot:{}};  /ngev okerjEa '<div class=""txn-l,fo"">',  f-lbl-w{}+'f-l /ng /ngev "Labe<span class=""txn-lth  "">Doot:</span>distoot+'&emsp;',  f-lbl-w{}+'f-l /ng /ngev "Labe<span class=""txn-lth  "">N)oure:</span>disn)oure+'&emsp;',  f-lbl-a,-2};  /){h.Cache[hvabe<span class=""txn-prict"">'+prict:{}p+'</span><br>',  f-lbl-w{}+'f-hv;  /ngev  Labe<span class=""txn-lth  "">Unit:</span>disunit+'&emsp;',  f-lbl-a,-2};  /){h.Cache "Labe<span class=""txn-lth  "">Flr:</span>disflr+'&emsp;',  f-lbl-a,-2};  /){h.Cache "Labe<span class=""txn-lth  "">Blk:</span>disblk+'&emsp;',  f-lbl-a,-2};  /){h.Cache "Labe<span class=""txn-lth  "">Ph"vl:</span>disphs+'&emsp;',  f-lbl-a,-2};  /){h.Cache "Labe<span class=""txn-lth  "">CP/PT:</span>discppt,  f-lbl-w{}+'f-l /ng /ngev "Labe</div>eekm /"vMath,foonhv;  /ngevin LCoaoJSON(gedx'aw{},foot){0]CurS0s "[0single-transa //  detail};  /){h.Cache[ur" //  00s "[Detail(txnTngetdi"v "  +'?overvhv;  /ngev  ncesf=txnTnge.split('|';  By haw{},foot:{}};  /ngev "v "cppt=f=tvrv-',unit=f=1]rv-',flr=(f[2]?f[2]th.F'oc-'; "e={driv v  " /ng /ngev "ncesblk=f=3]rv-',phs=f[4]rv-' "e={driv ,foot:{}};  /ngevRvtdfon)o
¶ ue.split('|';  By[6 By[6 By[6 By).sE]ic"vld){s;  /){h.Ca'dag /ngevin L;  /7{h.Cac;"&  vb ,l "v hv/ng /nwLeve
v>eekm /"vMa Bylit=h X" se  a &&f[12pslit=h X" }Cth  ]&prict:{}ppppp.f=l,fo.im)"?,foot:{}};  :/ngev "La"span>distoot+'&ems/ngevRvtdfocppt=id ;  /){h.Cache "Lsp;',5]{h.Cachass= /nwLeve!=='N/A'  /y[6 By[6v-',foosa,fo8">'parseFloat(f[8])>0? "La"span>dist.toLocaleS{}png()