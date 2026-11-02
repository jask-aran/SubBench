import { Site, mount } from "../app/shell";
import { link } from "../app/bits";
import { Button } from "../conduit/ui/controls";
import { DashEmpty } from "../conduit/ui/dashboard";

mount(() => <Site page="notfound" title="Not found">
  <DashEmpty>Page not found.</DashEmpty>
  <div class="sb-actions"><Button onClick={link("/")}>Overview</Button></div>
</Site>);
