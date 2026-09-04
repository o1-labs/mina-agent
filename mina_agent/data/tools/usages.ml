(* usages: every use of one binding across a set of compiled units.

   Reads the typed trees the compiler already wrote (.cmt / .cmti under
   _build), so a result is the typechecker's own resolution: there is no text
   matching, and ppx-generated references drop out on their ghost locations.

     usages <def_file> <line> <col>   < paths of .cmt/.cmti files, one per line

   Pass 1 reads the unit that owns <def_file> (its .cmt, and its .cmti when an
   interface exists) and finds the declaration whose name spans <line>:<col>,
   recording the location that references to it carry: val_loc for a value,
   cstr_loc for a constructor, lbl_loc for a record field, type_loc for a
   type. A binding declared in an .mli is seen by other units through that
   interface, so the matching declaration there (same kind, name, and module
   path) counts as the same binding. `include M` re-exports M's declarations
   at the include site, so a value defined in `module T` and exported by
   `include T` matches the interface's top-level declaration of it.

   Pass 2 walks every listed unit and reports each non-ghost reference whose
   carried location is one of those. A type reference is a path; it is
   resolved to its declaration in the environment the .cmt kept as a summary,
   rebuilt against the load path the .cmt recorded. Modules are not handled:
   a module reference is a prefix of a longident, not a node of its own.

   Output: one JSON object on stdout. Exit 2 when no declaration is at the
   position or the owning unit is not among the inputs. *)

open Typedtree

let def_file = Sys.argv.(1)

let def_line = int_of_string Sys.argv.(2)

let def_col = int_of_string Sys.argv.(3)

type kind = Value | Constructor | Label | Type

let kind_name = function
  | Value ->
      "value"
  | Constructor ->
      "constructor"
  | Label ->
      "label"
  | Type ->
      "type"

let ends_with ~suffix s =
  let ls = String.length s and lf = String.length suffix in
  ls >= lf && String.sub s (ls - lf) lf = suffix

(* a .cmt records its source as dune compiled it: relative to _build/default *)
let same_file a b = a = b || ends_with ~suffix:("/" ^ b) a || ends_with ~suffix:("/" ^ a) b

let col (p : Lexing.position) = p.pos_cnum - p.pos_bol + 1

type key = string * int * int

let key_of (l : Location.t) : key =
  (l.loc_start.pos_fname, l.loc_start.pos_lnum, col l.loc_start)

let key_json (f, l, c) = `Assoc [ ("file", `String f); ("line", `Int l); ("col", `Int c) ]

let spans_target (l : Location.t) =
  let s = l.loc_start and e = l.loc_end in
  (not l.loc_ghost)
  && same_file s.pos_fname def_file
  && (def_line > s.pos_lnum || (def_line = s.pos_lnum && def_col >= col s))
  && (def_line < e.pos_lnum || (def_line = e.pos_lnum && def_col <= col e))

let span_size (l : Location.t) = l.loc_end.pos_cnum - l.loc_start.pos_cnum

(* ---- pass 1: the declarations of one unit ---- *)

type decl =
  { kind : kind
  ; name : string
  ; path : string list
  ; toplevel : bool
  ; name_loc : Location.t
  ; key : key
  }

let default = Tast_iterator.default_iterator

let decls_of annots =
  let out = ref [] and path = ref [] and includes = ref [] in
  let add ?(toplevel = true) kind name name_loc key =
    out := { kind; name; path = List.rev !path; toplevel; name_loc; key } :: !out
  in
  let type_decls tds =
    List.iter
      (fun (td : type_declaration) ->
        add Type td.typ_name.txt td.typ_name.loc (key_of td.typ_type.type_loc) ;
        match (td.typ_kind, td.typ_type.type_kind) with
        | Ttype_variant cds, Types.Type_variant (tcds, _) ->
            List.iter
              (fun (cd : constructor_declaration) ->
                List.iter
                  (fun (t : Types.constructor_declaration) ->
                    if Ident.name t.cd_id = cd.cd_name.txt then
                      add Constructor cd.cd_name.txt cd.cd_name.loc (key_of t.cd_loc) )
                  tcds )
              cds
        | Ttype_record lds, Types.Type_record (tlds, _) ->
            List.iter
              (fun (ld : label_declaration) ->
                List.iter
                  (fun (t : Types.label_declaration) ->
                    if Ident.name t.ld_id = ld.ld_name.txt then
                      add Label ld.ld_name.txt ld.ld_name.loc (key_of t.ld_loc) )
                  tlds )
              lds
        | _ ->
            () )
      tds
  in
  (* a variable bound by a pattern: references carry the pattern node's loc *)
  let record_pat : type k. toplevel:bool -> k general_pattern -> unit =
   fun ~toplevel p ->
    match p.pat_desc with
    | Tpat_var (_, name) ->
        add ~toplevel Value name.txt name.loc (key_of p.pat_loc)
    | Tpat_alias (_, _, name) ->
        add ~toplevel Value name.txt name.loc (key_of p.pat_loc)
    | _ ->
        ()
  in
  let pit_top =
    { default with
      pat =
        (fun (type k) sub (p : k general_pattern) ->
          record_pat ~toplevel:true p ;
          default.pat sub p )
    }
  in
  let with_path (name : string option) f =
    match name with
    | Some n ->
        path := n :: !path ;
        f () ;
        path := List.tl !path
    | None ->
        f ()
  in
  let it =
    { default with
      pat =
        (fun (type k) sub (p : k general_pattern) ->
          record_pat ~toplevel:false p ;
          default.pat sub p )
    ; structure_item =
        (fun sub si ->
          match si.str_desc with
          | Tstr_value (_, vbs) ->
              List.iter
                (fun vb ->
                  pit_top.pat pit_top vb.vb_pat ;
                  sub.expr sub vb.vb_expr )
                vbs
          | Tstr_primitive vd ->
              add Value vd.val_name.txt vd.val_name.loc (key_of vd.val_val.val_loc)
          | Tstr_type (_, tds) ->
              type_decls tds
          | Tstr_include { incl_mod = { mod_desc = Tmod_ident (p, _); _ }; _ } ->
              (* declarations under (site @ M) are also visible at site *)
              let site = List.rev !path in
              includes := (site @ String.split_on_char '.' (Path.name p), site) :: !includes
          | _ ->
              default.structure_item sub si )
    ; signature_item =
        (fun sub si ->
          match si.sig_desc with
          | Tsig_value vd ->
              add Value vd.val_name.txt vd.val_name.loc (key_of vd.val_val.val_loc)
          | Tsig_type (_, tds) ->
              type_decls tds
          | _ ->
              default.signature_item sub si )
    ; module_binding =
        (fun sub mb -> with_path mb.mb_name.txt (fun () -> default.module_binding sub mb))
    ; module_declaration =
        (fun sub md ->
          with_path md.md_name.txt (fun () -> default.module_declaration sub md) )
    }
  in
  ( match annots with
  | Cmt_format.Implementation str ->
      it.structure it str
  | Cmt_format.Interface sg ->
      it.signature it sg
  | _ ->
      () ) ;
  (!out, List.rev !includes)

(* the identifier under the cursor in the source: ppx-derived bindings
   (equal, compare, b__008_ ...) share the name location of the declaration
   they derive from, so overlapping declarations are told apart by name *)
let ident_at_cursor () =
  try
    let ic = open_in def_file in
    let rec nth i =
      let l = input_line ic in
      if i = def_line then l else nth (i + 1)
    in
    let line = nth 1 in
    close_in ic ;
    let is_id c =
      c = '_' || c = '\'' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
    in
    let n = String.length line and c0 = def_col - 1 in
    if c0 < 0 || c0 >= n || not (is_id line.[c0]) then None
    else
      let s = ref c0 and e = ref c0 in
      while !s > 0 && is_id line.[!s - 1] do decr s done ;
      while !e < n - 1 && is_id line.[!e + 1] do incr e done ;
      Some (String.sub line !s (!e - !s + 1))
  with _ -> None

let debug = Sys.getenv_opt "USAGES_DEBUG" <> None

let find_target decls =
  let spanning = List.filter (fun d -> spans_target d.name_loc) decls in
  if debug then (
    Printf.eprintf "ident_at_cursor=%s decls=%d spanning=%d\n"
      (Option.value (ident_at_cursor ()) ~default:"<none>") (List.length decls) (List.length spanning) ;
    List.iter
      (fun d ->
        let s = d.name_loc.loc_start and e = d.name_loc.loc_end in
        Printf.eprintf "  %-11s %-20s path=%s top=%b ghost=%b %s:%d:%d-%d:%d\n" (kind_name d.kind) d.name
          (String.concat "." d.path) d.toplevel d.name_loc.loc_ghost s.pos_fname s.pos_lnum (col s) e.pos_lnum (col e) )
      spanning ;
    (* the declarations on the target line, whether or not they span it *)
    List.iter
      (fun d ->
        let s = d.name_loc.loc_start in
        if s.pos_lnum = def_line && not (List.memq d spanning) then
          Printf.eprintf "  (same line) %-11s %-20s ghost=%b col=%d\n" (kind_name d.kind) d.name
            d.name_loc.loc_ghost (col s) )
      decls ) ;
  (* with the token under the cursor known, a declaration must be named by
     it; a use of some other binding inside a declaration's span (a type
     mentioned in a type definition, say) is not a declaration *)
  let spanning =
    match ident_at_cursor () with
    | Some id ->
        List.filter (fun d -> d.name = id) spanning
    | None ->
        spanning
  in
  (* [@@deriving fields] and friends generate values whose name and location
     equal the label's or type's exactly; a genuine value never shares a name
     location with another kind of declaration, so on a tie the non-value
     declaration is the one written in the source *)
  let rank d = (span_size d.name_loc, match d.kind with Value -> 1 | _ -> 0) in
  match List.sort (fun a b -> compare (rank a) (rank b)) spanning with
  | [] ->
      None
  | d :: _ ->
      Some d

let rec drop_prefix pre l =
  match (pre, l) with
  | [], rest ->
      Some rest
  | p :: pre', x :: l' when p = x ->
      drop_prefix pre' l'
  | _ ->
      None

(* every module path a declaration is visible at: its own, plus the include
   sites that re-export the module it lives in *)
let exported_paths includes d =
  d.path
  :: List.filter_map
       (fun (inner, site) -> Option.map (fun rest -> site @ rest) (drop_prefix inner d.path))
       includes

let twins includes d others =
  if not d.toplevel then []
  else
    let mine = exported_paths includes d in
    List.filter
      (fun o ->
        o.toplevel && o.kind = d.kind && o.name = d.name
        && List.exists (fun p -> List.mem p (exported_paths includes o)) mine )
      others

(* the owning unit's .cmt/.cmti are recognised by module name: <stem>, or a
   wrapped library's <lib>__<Stem> *)
let stem = Filename.remove_extension (Filename.basename def_file)

let is_owner_path p =
  let b = Filename.remove_extension (Filename.basename p) in
  b = stem || b = String.capitalize_ascii stem || ends_with ~suffix:("__" ^ String.capitalize_ascii stem) b

(* dune compiles the ppx output, so a .cmt names its source x.pp.ml while
   every location inside it is remapped to x.ml *)
let source_stem f =
  let s = Filename.remove_extension f in
  if Filename.check_suffix s ".pp" then Filename.chop_suffix s ".pp" else s

let owner_source (cmt : Cmt_format.cmt_infos) =
  match cmt.cmt_sourcefile with
  | Some f when same_file (source_stem f) (Filename.remove_extension def_file) ->
      Some f
  | _ ->
      None

(* ---- pass 2: references in every unit ---- *)

let keys : (key, unit) Hashtbl.t = Hashtbl.create 8

let hits : (key, int) Hashtbl.t = Hashtbl.create 64

let unresolved = ref 0

let unresolved_by_file : (string, int) Hashtbl.t = Hashtbl.create 16

(* resolution of a globally-headed type path, by its name: None = unresolvable *)
let persistent_cache : (string, Location.t option) Hashtbl.t = Hashtbl.create 1024

(* debug timers: where a type query spends its time *)
let t_persistent = ref 0.0 and n_persistent = ref 0 and t_local = ref 0.0 and n_local = ref 0
and t_preread = ref 0.0 and t_read = ref 0.0

let record (lid : Longident.t Location.loc) (l : Location.t) =
  if (not lid.loc.loc_ghost) && Hashtbl.mem keys (key_of l) then
    let s = lid.loc.loc_start and e = lid.loc.loc_end in
    Hashtbl.replace hits (s.pos_fname, s.pos_lnum, col s) (e.pos_cnum - e.pos_bol)

let hit_iterator ~name want_types =
  { default with
    expr =
      (fun sub e ->
        ( match e.exp_desc with
        | Texp_ident (_, lid, vd) ->
            record lid vd.val_loc
        | Texp_construct (lid, cd, _) ->
            record lid cd.cstr_loc
        | Texp_field (_, lid, ld) | Texp_setfield (_, lid, ld, _) ->
            record lid ld.lbl_loc
        | Texp_record { fields; _ } ->
            Array.iter
              (fun ((ld : Types.label_description), def) ->
                match def with Overridden (lid, _) -> record lid ld.lbl_loc | Kept _ -> () )
              fields
        | _ ->
            () ) ;
        default.expr sub e )
  ; pat =
      (fun (type k) sub (p : k general_pattern) ->
        ( match p.pat_desc with
        | Tpat_construct (lid, cd, _, _) ->
            record lid cd.cstr_loc
        | Tpat_record (fields, _) ->
            List.iter (fun (lid, (ld : Types.label_description), _) -> record lid ld.lbl_loc) fields
        | _ ->
            () ) ;
        default.pat sub p )
  ; typ =
      (fun sub ct ->
        (* a reference to type [name] is written with [name] as the last
           component of its longident (type names cannot be aliased), so only
           those nodes need the environment rebuilt to resolve their path *)
        ( if want_types then
          match ct.ctyp_desc with
          | Ttyp_constr (path, lid, _) when (not lid.loc.loc_ghost) && Longident.last lid.txt = name
            -> (
              try
                (* a path headed by another compilation unit resolves from
                   that unit's .cmi alone, against the empty environment, and
                   the same path string names the same type wherever it is
                   written, so that resolution is memoised; only a local head
                   needs this file's environment rebuilt from its summary,
                   which is slow (it re-opens everything the file opened) and
                   the only thing that can fail *)
                let t0 = Unix.gettimeofday () in
                let persistent = Ident.persistent (Path.head path) in
                let loc =
                  if persistent then (
                    let k = Path.name path in
                    match Hashtbl.find_opt persistent_cache k with
                    | Some r ->
                        r
                    | None ->
                        let r = try Some (Env.find_type path Env.empty).type_loc with _ -> None in
                        Hashtbl.add persistent_cache k r ;
                        r )
                  else Some (Env.find_type path (Envaux.env_of_only_summary ct.ctyp_env)).type_loc
                in
                let dt = Unix.gettimeofday () -. t0 in
                if persistent then (t_persistent := !t_persistent +. dt ; incr n_persistent)
                else (t_local := !t_local +. dt ; incr n_local) ;
                match loc with Some l -> record lid l | None -> raise Not_found
              with exn ->
                incr unresolved ;
                let f = lid.loc.loc_start.pos_fname in
                Hashtbl.replace unresolved_by_file f
                  (1 + Option.value (Hashtbl.find_opt unresolved_by_file f) ~default:0) ;
                if debug && !unresolved <= 5 then
                  let why =
                    match exn with
                    | Envaux.Error (Envaux.Module_not_found p) ->
                        "module not found: " ^ Path.name p
                    | e ->
                        Printexc.to_string e
                  in
                  Printf.eprintf "  unresolved %s at %s:%d: %s\n" (Path.name path)
                    lid.loc.loc_start.pos_fname lid.loc.loc_start.pos_lnum why )
          | _ ->
              () ) ;
        default.typ sub ct )
  }

let () =
  let inputs = ref [] in
  ( try
      while true do
        let l = String.trim (input_line stdin) in
        if l <> "" then inputs := l :: !inputs
      done
    with End_of_file -> () ) ;
  let inputs = List.rev !inputs in
  (* pass 1 *)
  let ml = ref None and mli = ref None in
  List.iter
    (fun p ->
      if is_owner_path p then
        match Cmt_format.read_cmt p with
        | exception _ ->
            ()
        | cmt -> (
            match owner_source cmt with
            | Some f when Filename.check_suffix f ".mli" ->
                mli := Some (decls_of cmt.cmt_annots)
            | Some _ ->
                ml := Some (decls_of cmt.cmt_annots)
            | None ->
                () ) )
    inputs ;
  let fail msg =
    print_string (Yojson.Safe.to_string (`Assoc [ ("error", `String msg) ])) ;
    exit 2
  in
  let here, there =
    if Filename.check_suffix def_file ".mli" then (!mli, !ml) else (!ml, !mli)
  in
  let here, includes =
    match (here, !ml) with
    | Some (d, _), Some (_, incl) ->
        (d, incl)
    | Some (d, _), None ->
        (d, [])
    | None, _ ->
        fail (Printf.sprintf "the unit owning %s is not compiled (no .cmt among the inputs)" def_file)
  in
  let target =
    match find_target here with
    | Some d ->
        d
    | None ->
        fail (Printf.sprintf "no declaration spans %s:%d:%d" def_file def_line def_col)
  in
  let there = match there with Some (d, _) -> d | None -> [] in
  let ids = target :: twins includes target there in
  List.iter (fun d -> Hashtbl.replace keys d.key ()) ids ;
  (* pass 2 *)
  let want_types = target.kind = Type in
  (* Resolving a type path needs the .cmi files of the units its environment
     mentions. Persistent_env memoises a failed lookup as missing for good, so
     the load path cannot be switched per unit: use the union of every input
     unit's recorded load path, set once. Unit names are unique across a dune
     workspace, so the union is unambiguous. *)
  if want_types then (
    let t0 = Unix.gettimeofday () in
    at_exit (fun () -> if debug then Printf.eprintf "  preread=%.2fs read=%.2fs persistent=%d in %.2fs local=%d in %.2fs\n"
        !t_preread !t_read !n_persistent !t_persistent !n_local !t_local) ;
    let seen = Hashtbl.create 256 and dirs = ref [] in
    List.iter
      (fun p ->
        match Cmt_format.read_cmt p with
        | exception _ ->
            ()
        | cmt ->
            List.iter
              (fun d ->
                let d = if Filename.is_relative d then Filename.concat cmt.cmt_builddir d else d in
                if not (Hashtbl.mem seen d) then (
                  Hashtbl.add seen d () ;
                  dirs := d :: !dirs ) )
              cmt.cmt_loadpath )
      inputs ;
    Load_path.init (List.rev !dirs) ;
    t_preread := Unix.gettimeofday () -. t0 ) ;
  let it = hit_iterator ~name:target.name want_types in
  let files_read = ref 0 and unreadable = ref [] in
  List.iter
    (fun p ->
      let t0 = Unix.gettimeofday () in
      match Cmt_format.read_cmt p with
      | exception _ ->
          unreadable := p :: !unreadable
      | cmt -> (
          t_read := !t_read +. (Unix.gettimeofday () -. t0) ;
          incr files_read ;
          if want_types then Envaux.reset_cache () ;
          match cmt.cmt_annots with
          | Cmt_format.Implementation str ->
              it.structure it str
          | Cmt_format.Interface sg ->
              it.signature it sg
          | _ ->
              () ) )
    inputs ;
  let usages =
    Hashtbl.fold (fun (f, l, c) e acc -> (f, l, c, e) :: acc) hits []
    |> List.sort compare
    |> List.map (fun (f, l, c, e) ->
           `Assoc
             [ ("file", `String f); ("line", `Int l); ("col", `Int c); ("end_col", `Int e) ] )
  in
  print_string
    (Yojson.Safe.to_string
       (`Assoc
         [ ( "target"
           , `Assoc
               [ ("kind", `String (kind_name target.kind))
               ; ("name", `String target.name)
               ; ("module_path", `List (List.map (fun s -> `String s) target.path))
               ; ("definitions", `List (List.map (fun d -> key_json d.key) ids))
               ] )
         ; ("usages", `List usages)
         ; ("files_read", `Int !files_read)
         ; ("unreadable", `List (List.map (fun s -> `String s) (List.rev !unreadable)))
         ; ("unresolved_types", `Int !unresolved)
         ; ( "unresolved_files"
           , `List
               ( Hashtbl.fold (fun f n acc -> (n, f) :: acc) unresolved_by_file []
               |> List.sort (fun a b -> compare b a)
               |> List.filteri (fun i _ -> i < 10)
               |> List.map (fun (n, f) -> `Assoc [ ("file", `String f); ("count", `Int n) ]) ) )
         ] ) )
